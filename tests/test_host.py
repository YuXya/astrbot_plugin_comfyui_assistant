"""Exercise the real host types with isolated contexts and no platform connection."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot.api.message_components import Image, Plain
from astrbot.api.provider import LLMResponse
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.message.message_event_result import MessageEventResult


@pytest.fixture
def module():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("isolated_comfy_plugin", root / "main.py", submodule_search_locations=[str(root)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class Event:
    def __init__(self):
        self.extras = {}
        self.result = MessageEventResult(chain=[Plain("progress")])
        self.unified_msg_origin = "qq:GroupMessage:100"
        self.message_obj = SimpleNamespace(message=[], message_id="fixture")

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_result(self):
        return self.result

    def get_sender_id(self):
        return "fixture-user"


async def test_host_response_observer_preserves_payload_and_other_turns(module, monkeypatch):
    event = Event()
    response = LLMResponse(role="tool", completion_text="progress", tools_call_name=["comfyui_generate"], tools_call_args=[{}], tools_call_ids=["call"])
    async def original(runner, *args, **kwargs):
        yield response
    monkeypatch.setattr(ToolLoopAgentRunner, "_iter_llm_responses", original)
    plugin = object.__new__(module.Main)
    plugin.runtime = SimpleNamespace(stopped=False)
    plugin.install_response_observer()
    try:
        await plugin.agent_begin(event, None)
        runner = SimpleNamespace(run_context=SimpleNamespace(context=SimpleNamespace(event=event)))
        observed = [r async for r in ToolLoopAgentRunner._iter_llm_responses(runner)]
        assert observed[0] is response and response.completion_text == "progress"
        await plugin.decorate(event)
        assert event.result.chain == []
        event.set_extra("cwa_intermediate", False)
        event.set_extra("cwa_owned", True)
        event.result.chain = [Plain("redundant closing text")]
        await plugin.decorate(event)
        assert not event.result.chain
        unrelated = Event()
        await plugin.decorate(unrelated)
        assert unrelated.result.get_plain_text() == "progress"
        event.set_extra("cwa_mixed", True)
        event.result.chain = [Plain("weather result")]
        await plugin.decorate(event)
        assert event.result.get_plain_text() == "weather result"
    finally:
        ToolLoopAgentRunner._iter_llm_responses = original


async def test_host_sender_checks_false_and_components(module, tmp_path):
    received = []
    async def send(scope, chain):
        received.append((scope, chain))
        return False
    plugin = object.__new__(module.Main)
    plugin.context = SimpleNamespace(send_message=send)
    f = tmp_path / "fixture.png"
    f.write_bytes(b"fixture")
    assert await plugin.send("qq:FriendMessage:1", "caption", [str(f)]) is False
    assert [type(x) for x in received[0][1].chain] == [Image, Plain]


async def test_current_and_quoted_images_preserve_order(module):
    plugin = object.__new__(module.Main)
    plugin.context = SimpleNamespace()
    event = Event()
    event.message_obj.message = [Image.fromURL("https://example.test/a.png"), Image.fromURL("https://example.test/b.png")]
    assert await plugin.candidates(event) == ["https://example.test/a.png", "https://example.test/b.png"]
    event.message_obj.message = [SimpleNamespace(chain=[Image.fromURL("https://example.test/quoted.png")])]
    assert await plugin.candidates(event) == ["https://example.test/quoted.png"]


async def test_qq_tool_images_in_current_agent_context(module):
    from astrbot.core.agent.message import ImageURLPart, UserMessageSegment
    plugin = object.__new__(module.Main)
    plugin.context = SimpleNamespace()
    a, b = "https://example.test/bot.png", "https://example.test/user.png"
    message = UserMessageSegment(content=[ImageURLPart(image_url=ImageURLPart.ImageURL(url=a)), ImageURLPart(image_url=ImageURLPart.ImageURL(url=b))])
    assert await plugin.candidates(Event(), [message]) == [a, b]


async def test_tool_schema_real_host_types(module):
    tool = module.AssistantTool(name="comfyui_workflows", description="fixture", parameters=module.object_schema(), plugin=None)
    assert tool.name == "comfyui_workflows"
    assert tool.parameters["additionalProperties"] is False


async def test_text_only_does_not_need_chat_history(module):
    async def create(*args, **kwargs):
        assert kwargs["candidates"] == []
        return {"id": "fixture"}
    plugin = object.__new__(module.Main)
    plugin.runtime = SimpleNamespace(store=SimpleNamespace(workflows=lambda: [{"id": "text", "name": "text", "bindings": {"images": []}}]), create=create)
    result = await plugin.dispatch("comfyui_generate", Event(), {"workflow_name": "text", "texts": ["teapot"]})
    assert result["task"]["id"] == "fixture"


async def test_initialize_and_unload_preserve_foreign_routes_and_tools(module, tmp_path, monkeypatch):
    context = SimpleNamespace(registered_web_apis=[("/foreign", None, ["GET"], "fixture")], provider_manager=SimpleNamespace(llm_tools=SimpleNamespace(func_list=[])))
    context.add_llm_tools = lambda *items: context.provider_manager.llm_tools.func_list.extend(items)
    context.register_web_api = lambda *args: context.registered_web_apis.append(args)
    monkeypatch.setattr(module.StarTools, "get_data_dir", lambda *args: tmp_path)
    plugin = module.Main(context)
    original = ToolLoopAgentRunner._iter_llm_responses
    await plugin.initialize()
    assert {t.name for t in context.provider_manager.llm_tools.func_list} == module.TOOLS
    assert len(context.registered_web_apis) == 3
    generate = next(t for t in context.provider_manager.llm_tools.func_list if t.name == "comfyui_generate")
    assert set(generate.parameters["properties"]) == {"workflow_name", "texts", "image_urls", "width", "height", "caption"}
    assert "前缀" in generate.parameters["properties"]["texts"]["description"]
    assert "默认提示词前缀由程序自动添加" in module.GUIDANCE
    await plugin.terminate()
    assert context.registered_web_apis == [("/foreign", None, ["GET"], "fixture")]
    assert not context.provider_manager.llm_tools.func_list
    assert ToolLoopAgentRunner._iter_llm_responses is original
