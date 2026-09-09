import asyncio
import base64
import copy
import io
import json
import time

import httpx
import pytest
from PIL import Image

from assistant.backend import Backend, Rejected
from assistant.catalog import DEFAULTS, build_graph, dimensions, render_start_message, settings_checked, workflow_checked
from assistant.media import Media
from assistant.runtime import Runtime


def png(color="white"):
    f = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(f, format="PNG")
    return f.getvalue()


def workflow(images=0):
    graph = {"text": {"class_type": "Simple String", "inputs": {"string": "original"}}, "size": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}}, "sampler": {"class_type": "KSampler", "inputs": {"seed": 12, "steps": 8, "cfg": 1, "latent_image": ["size", 0]}}, "save": {"class_type": "SaveImage", "inputs": {"images": ["sampler", 0]}}}
    for n in range(images):
        graph[f"image{n}"] = {"class_type": "ETN_LoadImageBase64", "inputs": {"image": ""}}
    return workflow_checked({"name": "测试工作流", "graph": graph})


class FakeBackend:
    def __init__(self):
        self.submissions = []
        self.ready = True
        self.uncertain = False
        self.reject = False
        self.failure = False
        self.found = True

    async def submit(self, server, graph, identifier):
        self.submissions.append((server, graph, identifier))
        if self.reject:
            raise Rejected("模型文件不存在")
        if self.uncertain:
            raise TimeoutError("response lost")
        return "prompt-" + identifier

    async def find_submission(self, server, identifier):
        return "prompt-" + identifier if self.found else None

    async def history(self, server, identifier):
        if not self.ready:
            return None
        if self.failure:
            return {"status": {"status_str": "error", "messages": [["execution_error", {"exception_message": "execution failed"}]]}}
        return {"status": {"status_str": "success", "completed": True}, "outputs": {"save": {"images": [{"filename": "fixture.png", "type": "output"}]}, "internal": {"images": [{"filename": "ignore.png", "type": "temp"}]}}}

    async def download(self, server, descriptor):
        return png()

    async def close(self):
        pass


class Sender:
    def __init__(self, outcome=True):
        self.calls = []
        self.outcome = outcome

    async def __call__(self, scope, text, paths):
        self.calls.append((scope, text, paths))
        if paths and isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome if paths else True


async def completed(rt):
    while rt.workers:
        await asyncio.gather(*list(rt.workers.values()))
        await asyncio.sleep(0)


def runtime(tmp_path, outcome=True, count=0):
    sender, backend = Sender(outcome), FakeBackend()
    rt = Runtime(tmp_path, sender, backend=backend)
    rt.settings["poll_seconds"] = 0.01
    rt.save_workflow(workflow(count))
    return rt, sender, backend


async def create(rt, **extra):
    return await rt.create("qq:GroupMessage:100", "user1", "message1", "测试工作流", ["simple test"], **extra)


async def test_early_llm_exit_is_independent_and_two_messages(tmp_path):
    rt, sender, backend = runtime(tmp_path)
    try:
        result = await create(rt, width=768, height=1024)
        assert result["generation"] == "queued"
        await completed(rt)
        job = rt.store.task(result["id"])
        assert (job["generation"], job["delivery"]) == ("completed", "sent")
        assert len(sender.calls) == 2 and not sender.calls[0][2] and len(sender.calls[1][2]) == 1
        assert len(backend.submissions) == 1
        for _ in range(3):
            rt.tasks("qq:GroupMessage:100", "user1")
            rt.refresh(job["id"], "qq:GroupMessage:100", "user1")
        assert len(sender.calls) == 2
    finally:
        await rt.stop()


@pytest.mark.parametrize("outcome,status", [(False, "failed"), (Rejected("refused"), "failed"), (TimeoutError("uncertain"), "unknown"), (RuntimeError("disconnect"), "unknown")])
async def test_failed_delivery_keeps_image_and_resend_never_regenerates(tmp_path, outcome, status):
    rt, sender, backend = runtime(tmp_path, outcome)
    try:
        result = await create(rt)
        await completed(rt)
        job = rt.store.task(result["id"])
        assert job["generation"] == "completed" and job["delivery"] == status
        assert (rt.root / job["outputs"][0]).is_file()
        assert rt.tasks("qq:GroupMessage:100", "user1")[0]["id"] == job["id"]
        sender.outcome = True
        await rt.resend(job["id"], "qq:GroupMessage:100", "user1")
        assert rt.store.task(job["id"])["delivery"] == "sent"
        assert len(backend.submissions) == 1
        assert len([c for c in sender.calls if c[2]]) == 2
    finally:
        await rt.stop()


async def test_idempotent_same_event_and_scope_isolation(tmp_path):
    rt, sender, backend = runtime(tmp_path)
    try:
        a, b = await asyncio.gather(create(rt), create(rt))
        assert a["id"] == b["id"]
        await completed(rt)
        assert len(backend.submissions) == 1
        assert not rt.tasks("qq:FriendMessage:100", "user1")
        assert not rt.tasks("qq:GroupMessage:100", "user2")
        for scope, owner in (("qq:FriendMessage:100", "user1"), ("qq:GroupMessage:100", "user2")):
            with pytest.raises(ValueError):
                await rt.resend(a["id"], scope, owner)
            with pytest.raises(ValueError):
                rt.media_reference(f"comfy-media:{a['id']}:0", scope, owner)
            path = rt.root / rt.store.task(a["id"])["outputs"][0]
            with pytest.raises(ValueError):
                await rt.media.read(str(path), scope, owner)
    finally:
        await rt.stop()


async def test_restart_recovers_generation_without_resubmit(tmp_path):
    rt, sender, backend = runtime(tmp_path)
    backend.ready = False
    result = await create(rt)
    while not backend.submissions:
        await asyncio.sleep(0.01)
    await rt.stop()
    fresh = Runtime(tmp_path, sender, backend=backend)
    backend.ready = True
    try:
        await fresh.start()
        await completed(fresh)
        assert fresh.store.task(result["id"])["delivery"] == "sent"
        assert len(backend.submissions) == 1 and len(sender.calls) == 2
    finally:
        await fresh.stop()


async def test_restart_during_send_marks_unknown_without_resend(tmp_path):
    rt, sender, backend = runtime(tmp_path)
    result = await create(rt)
    await completed(rt)
    job = rt.store.task(result["id"])
    job["delivery"] = "sending"
    job["attempts"][-1]["status"] = "sending"
    rt.store.save_task(job)
    await rt.stop()
    fresh = Runtime(tmp_path, sender, backend=backend)
    try:
        await fresh.start()
        await completed(fresh)
        assert fresh.store.task(job["id"])["delivery"] == "unknown"
        assert len(sender.calls) == 2
    finally:
        await fresh.stop()


@pytest.mark.parametrize("found", [True, False])
async def test_uncertain_submission_is_reconciled_not_repeated(tmp_path, found):
    rt, sender, backend = runtime(tmp_path)
    backend.uncertain, backend.found = True, found
    try:
        result = await create(rt)
        await completed(rt)
        job = rt.store.task(result["id"])
        assert job["generation"] == ("completed" if found else "submission_unknown")
        rt.refresh(job["id"])
        await completed(rt)
        assert len(backend.submissions) == 1
    finally:
        await rt.stop()


@pytest.mark.parametrize("submit_reject", [True, False])
async def test_generation_failure_is_not_delivery_success(tmp_path, submit_reject):
    rt, sender, backend = runtime(tmp_path)
    backend.reject, backend.failure = submit_reject, not submit_reject
    try:
        result = await create(rt)
        await completed(rt)
        job = rt.store.task(result["id"])
        assert job["generation"] == "failed" and job["delivery"] != "sent"
        assert len(sender.calls) == 2 and not any(c[2] for c in sender.calls)
    finally:
        await rt.stop()


async def test_expiry_keeps_status_and_cannot_resend(tmp_path):
    rt, sender, backend = runtime(tmp_path)
    try:
        result = await create(rt)
        await completed(rt)
        job = rt.store.task(result["id"])
        job["generated_at"] = time.time() - 8 * 86400
        rt.store.save_task(job)
        rt.cleanup()
        assert rt.store.task(job["id"])["files_expired"]
        with pytest.raises(ValueError, match="过期"):
            await rt.resend(job["id"])
        assert len(backend.submissions) == 1
    finally:
        await rt.stop()


@pytest.mark.parametrize("width,height", [(256, 256), (768, 1024), (1536, 1024), (2048, 1024), (None, None)])
def test_dimensions_and_other_parameters_preserved(width, height):
    w = workflow(2)
    original = copy.deepcopy(w)
    graph = build_graph(w, ["updated"], ["image-one", "image-two"], width, height, DEFAULTS)
    assert w == original
    assert graph["size"]["inputs"]["width"] == (width or 1024)
    assert graph["size"]["inputs"]["height"] == (height or 1024)
    assert graph["sampler"]["inputs"]["steps"] == 8
    assert graph["sampler"]["inputs"]["cfg"] == 1
    assert graph["image0"]["inputs"]["image"] == "image-one"
    assert graph["image1"]["inputs"]["image"] == "image-two"


@pytest.mark.parametrize("width,height", [(None, 1024), (-8, 1024), (1025, 1024), (2048, 2048), (True, 1024), (1024.0, 1024)])
def test_bad_size_rejected(width, height):
    with pytest.raises(ValueError):
        dimensions(width, height, DEFAULTS, workflow()["bindings"])


async def test_media_placeholder_array_order_and_missing_inputs(tmp_path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    a.write_bytes(png("red"))
    b.write_bytes(png("blue"))
    media = Media([tmp_path], lambda *x: "")
    placeholder = "base64://ASTRBOT_PLUGIN_CACHE_PENDING"
    images = await media.resolve([placeholder, placeholder], [str(a), str(b)], 2, "scope", "owner")
    assert base64.b64decode(images[0]) == a.read_bytes()
    assert base64.b64decode(images[1]) == b.read_bytes()
    mixed = await media.resolve([str(a), placeholder], [str(a), str(b)], 2, "scope", "owner")
    assert base64.b64decode(mixed[1]) == b.read_bytes()
    with pytest.raises(ValueError, match="不会重复"):
        await media.resolve([placeholder, placeholder], [str(a)], 2, "scope", "owner")
    with pytest.raises(ValueError, match="需要明确"):
        await media.resolve([], [str(a), str(b)], 1, "scope", "owner")
    with pytest.raises(ValueError, match="允许"):
        await media.read(str(tmp_path.parent / "outside.png"), "scope", "owner")
    assert await media.read(a.as_uri(), "scope", "owner") == a.read_bytes()


def test_workflow_validation_and_settings():
    with pytest.raises(ValueError):
        workflow_checked({"name": "bad", "graph": {"nodes": []}})
    w = workflow()
    w["bindings"]["height"] = []
    with pytest.raises(ValueError):
        workflow_checked(w)
    assert settings_checked({"server_url": "127.0.0.1:8188"})["server_url"].startswith("http://")
    with pytest.raises(ValueError):
        settings_checked({"server_url": "file:///test"})
    with pytest.raises(ValueError):
        settings_checked({"image_days": 99, "record_days": 7})


async def test_backend_submission_identity_and_history_contract():
    requests = []
    def respond(request):
        requests.append(request)
        if request.url.path == "/prompt":
            body = json.loads(request.content)
            assert body["extra_data"]["comfyui_assistant_task"] == "job"
            return httpx.Response(200, json={"prompt_id": "pid"})
        if request.url.path == "/queue":
            return httpx.Response(200, json={"queue_running": [[1, "pid", {}, {"comfyui_assistant_task": "job"}, []]]})
        return httpx.Response(200, json={"pid": {"status": {"completed": True}}})
    backend = Backend()
    await backend.client.aclose()
    backend.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        assert await backend.submit("http://example.test", {}, "job") == "pid"
        assert await backend.find_submission("http://example.test", "job") == "pid"
        assert (await backend.history("http://example.test", "pid"))["status"]["completed"]
    finally:
        await backend.close()


@pytest.mark.parametrize("prefix,expected", [
    ("", "金发碧眼猫娘"),
    ("  \n", "金发碧眼猫娘"),
    ("masterpiece, best quality,", "masterpiece, best quality,\n金发碧眼猫娘"),
    ("高质量，\nsource_anime,", "高质量，\nsource_anime,\n金发碧眼猫娘"),
    ("highres,\r\n", "highres,\n金发碧眼猫娘"),
])
def test_prefix_is_applied_only_to_request_copy(prefix, expected):
    w = workflow(2)
    w["prompt_prefix"] = prefix
    original = copy.deepcopy(w)
    texts = ["金发碧眼猫娘"]
    graph = build_graph(w, texts, ["first-image", "second-image"], 768, 1024, DEFAULTS)
    assert graph["text"]["inputs"]["string"] == expected
    assert texts == ["金发碧眼猫娘"] and w == original
    assert graph["image0"]["inputs"]["image"] == "first-image"
    assert graph["image1"]["inputs"]["image"] == "second-image"
    expected_graph = copy.deepcopy(w["graph"])
    expected_graph["text"]["inputs"]["string"] = expected
    expected_graph["image0"]["inputs"]["image"] = "first-image"
    expected_graph["image1"]["inputs"]["image"] = "second-image"
    expected_graph["size"]["inputs"].update(width=768, height=1024)
    expected_graph["sampler"]["inputs"]["seed"] = graph["sampler"]["inputs"]["seed"]
    assert graph == expected_graph


def test_prefix_legacy_defaults_and_stable_target():
    w = workflow()
    w.pop("prompt_prefix")
    w.pop("prefix_target")
    legacy = copy.deepcopy(w)
    normalized = workflow_checked(w)
    assert normalized["prompt_prefix"] == ""
    assert normalized["prefix_target"] == {"node": "text", "input": "string"}
    assert w == legacy
    w = normalized
    w["graph"]["negative"] = {"class_type": "Simple String", "inputs": {"string": "negative default"}}
    w["bindings"]["texts"].insert(0, {"node": "negative", "input": "string", "label": "负面提示词"})
    w["prompt_prefix"] = "high quality,"
    graph = build_graph(w, ["bad anatomy", "cat"], [], None, None, DEFAULTS)
    assert graph["negative"]["inputs"]["string"] == "bad anatomy"
    assert graph["text"]["inputs"]["string"] == "high quality,\ncat"
    w["bindings"]["texts"].pop()
    with pytest.raises(ValueError, match="前缀应用位置已失效"):
        workflow_checked(w)


@pytest.mark.parametrize("value", [None, 123, [], "x" * 20001])
def test_prefix_rejects_invalid_configuration(value):
    w = workflow()
    w["prompt_prefix"] = value
    with pytest.raises(ValueError, match="默认提示词前缀必须"):
        workflow_checked(w)


def test_prefix_requires_text_target_and_enforces_combined_limit():
    w = workflow()
    w["prompt_prefix"] = "quality"
    w["bindings"]["texts"] = []
    with pytest.raises(ValueError, match="先绑定文字入口"):
        workflow_checked(w)
    w["prompt_prefix"] = ""
    assert workflow_checked(w)["prefix_target"] is None
    w = workflow()
    w["prompt_prefix"] = "x" * 10000
    assert len(build_graph(w, ["y" * 9999], [], None, None, DEFAULTS)["text"]["inputs"]["string"]) == 20000
    with pytest.raises(ValueError, match="合计不能超过"):
        build_graph(w, ["y" * 10000], [], None, None, DEFAULTS)


@pytest.mark.parametrize("phase", ["queued", "running"])
async def test_prefix_snapshot_survives_config_edit_restart_queries_and_resend(tmp_path, monkeypatch, phase):
    rt, sender, backend = runtime(tmp_path)
    backend.ready = False
    w = rt.store.workflows()[0]
    w["prompt_prefix"] = "original prefix,"
    rt.save_workflow(w)
    if phase == "queued":
        monkeypatch.setattr(rt, "spawn", lambda identifier: None)
    try:
        result = await create(rt)
        if phase == "running":
            async with asyncio.timeout(3):
                while not backend.submissions:
                    await asyncio.sleep(0.01)
        request_file = tmp_path / "tasks" / result["id"] / "request.json"
        original = request_file.read_bytes()
        assert json.loads(original)["text"]["inputs"]["string"] == "original prefix,\nsimple test"
        w["prompt_prefix"] = "new prefix,"
        rt.save_workflow(w)
        # A repeat of the same event must retain the original saved request.
        assert (await create(rt))["id"] == result["id"]
        assert request_file.read_bytes() == original
    finally:
        await rt.stop()
    backend.ready = True
    fresh = Runtime(tmp_path, sender, backend=backend)
    try:
        assert fresh.store.workflows()[0]["prompt_prefix"] == "new prefix,"
        await fresh.start()
        await completed(fresh)
        assert len(backend.submissions) == 1
        assert backend.submissions[0][1]["text"]["inputs"]["string"] == "original prefix,\nsimple test"
        assert len(sender.calls) == 2
        assert fresh.store.task(result["id"])["workflow_snapshot"]["prompt_prefix"] == "original prefix,"
        for _ in range(3):
            fresh.refresh(result["id"])
            fresh.tasks("qq:GroupMessage:100", "user1")
        assert len(sender.calls) == 2 and request_file.read_bytes() == original
        await fresh.resend(result["id"])
        assert len(backend.submissions) == 1 and len(sender.calls) == 3
        assert request_file.read_bytes() == original
        await fresh.create("qq:GroupMessage:100", "user1", "message2", w["name"], ["second scene"])
        await completed(fresh)
        assert len(backend.submissions) == 2
        assert backend.submissions[1][1]["text"]["inputs"]["string"] == "new prefix,\nsecond scene"
    finally:
        await fresh.stop()


@pytest.mark.parametrize("scale,width,height,expected", [
    (1,1280,720,(1280,720)), (0.5,1280,720,(640,360)),
    (0.25,1280,768,(320,192)), (0.1,1280,720,(128,72)),
    (2,512,512,(1024,1024)), (0.5,None,None,(1024,1024)),
    (0.3,1280,728,(384,216)), (0.25,1280,720,(320,184)),
    (0.001,512,512,(8,8)), (0.5,1280,728,(640,368)),
])
def test_size_scale_exact_values_and_template_preservation(scale, width, height, expected):
    w = workflow(2)
    w["size_scale"] = scale
    original = copy.deepcopy(w)
    graph = build_graph(w,["scene"],["one","two"],width,height,DEFAULTS)
    assert (graph["size"]["inputs"]["width"],graph["size"]["inputs"]["height"]) == expected
    assert w == original
    assert graph["image0"]["inputs"]["image"] == "one"
    assert graph["image1"]["inputs"]["image"] == "two"
    assert graph["sampler"]["inputs"]["steps"] == original["graph"]["sampler"]["inputs"]["steps"]


@pytest.mark.parametrize("value", [0,-0.5,True,"0.5",None,float("inf"),float("nan")])
def test_size_scale_rejects_invalid_values(value):
    w=workflow()
    w["size_scale"]=value
    with pytest.raises(ValueError,match="图片大小写入缩放"):
        workflow_checked(w)


@pytest.mark.parametrize("scale,width,height", [(8,512,512),(2,1280,720)])
def test_size_scale_keeps_resource_limits(scale,width,height):
    w=workflow()
    w["size_scale"]=scale
    with pytest.raises(ValueError,match="缩放"):
        build_graph(w,["scene"],[],width,height,DEFAULTS)


@pytest.mark.parametrize("template", ["", "   ", "{unknown}", "{width.__class__}", "{size!r}", "{width:10000000}", "{", "x"*1001, None])
def test_start_template_validation(template):
    with pytest.raises(ValueError,match="开始提示"):
        settings_checked({"start_message_template":template})


def test_start_template_renders_original_size_and_default_fallback():
    template="在画了老大，大小：{size} / {width}×{height} / {workflow} / {{完成}}"
    assert render_start_message(template,1280,720,"MM") == "在画了老大，大小：1280×720 / 1280×720 / MM / {完成}"
    assert render_start_message("在画了老大，大小：{size}",None,None,"MM") == "在画了老大，大小：默认尺寸"
    assert render_start_message("{width}×{height}",None,None,"MM") == "默认×默认"
    assert settings_checked({})["start_message_template"] == DEFAULTS["start_message_template"]
    w=workflow()
    w.pop("size_scale")
    assert workflow_checked(w)["size_scale"]==1


async def test_scaled_task_and_start_message_snapshots_survive_restart_and_resend(tmp_path,monkeypatch):
    rt,sender,backend=runtime(tmp_path)
    w=rt.store.workflows()[0]
    w["size_scale"]=0.5
    rt.save_workflow(w)
    rt.configure({"poll_seconds":1,"start_message_template":"在画了老大，大小：{size}"})
    monkeypatch.setattr(rt,"spawn",lambda identifier:None)
    try:
        result=await create(rt,width=1280,height=720)
        assert (result["width"],result["height"])==(1280,720)
        assert (result["input_width"],result["input_height"])==(640,360)
        request_file=tmp_path/"tasks"/result["id"]/"request.json"
        request_bytes=request_file.read_bytes()
        w["size_scale"]=1
        rt.save_workflow(w)
        rt.configure({"start_message_template":"新提示：{size}"})
    finally:
        await rt.stop()
    fresh=Runtime(tmp_path,sender,backend=backend)
    try:
        await fresh.start()
        await completed(fresh)
        assert sender.calls[0][1]=="在画了老大，大小：1280×720"
        assert len(sender.calls)==2 and len(backend.submissions)==1
        graph=backend.submissions[0][1]
        assert graph["size"]["inputs"]["width"]==640 and graph["size"]["inputs"]["height"]==360
        fresh.refresh(result["id"])
        fresh.tasks("qq:GroupMessage:100","user1")
        await fresh.resend(result["id"])
        assert len(sender.calls)==3 and len(backend.submissions)==1
        assert request_file.read_bytes()==request_bytes
        await fresh.create("qq:GroupMessage:100","user1","message2",w["name"],["new scene"],width=1280,height=720)
        await completed(fresh)
        assert sender.calls[3][1]=="新提示：1280×720"
        assert backend.submissions[1][1]["size"]["inputs"]["width"]==1280
    finally:
        await fresh.stop()
