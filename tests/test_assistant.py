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
from assistant.catalog import DEFAULTS, build_graph, dimensions, settings_checked, workflow_checked
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
