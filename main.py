"""AstrBot tools, authenticated management page, and scoped reply handling."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import FunctionTool
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, json_response, request
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .assistant import __version__
from .assistant.backend import Rejected
from .assistant.runtime import Runtime


PLUGIN = "astrbot_plugin_comfyui_assistant"
TOOLS = {"comfyui_workflows", "comfyui_generate", "comfyui_tasks", "comfyui_resend"}
MEDIA_TOOLS = {"get_image_from_context", "get_message_detail", "qts_get_message_detail", "get_recent_messages", "qts_get_recent_messages", "get_user_info", "qts_get_user_info", "view_qq_avatar", "qts_view_qq_avatar", "astrbot_file_read_tool"}
GUIDANCE = """ComfyUI 绘图助手：先按工作流说明和槽位准备文字及图片。工作流配置的默认提示词前缀由程序自动添加，texts 只填写本次描述或修改要求，无需重复填写已配置的前缀。根据用户明确尺寸或用途选择宽高，成对填写并遵守工具返回的尺寸范围；不要修改模型、采样等参数。comfyui_generate 创建任务后，插件会自动发送开始提示和最终图片，不需要你轮询、读取生成图或调用其他发送工具。本轮不要再输出绘图进度或重复配文。用户询问进度用 comfyui_tasks；用户明确要求重发时用 comfyui_resend，不能以重新生成代替重发。生成完成与发送成功是不同状态，不能把提交或读图说成已发送。生成参数不明确或图片缺失时正常询问。"""


@dataclass
class AssistantTool(FunctionTool[AstrAgentContext]):
    plugin: Any = None

    async def call(self, context, **kwargs):
        try:
            return json.dumps(await self.plugin.dispatch(self.name, context.context.event, kwargs, getattr(context, "messages", ())), ensure_ascii=False)
        except Exception as exc:
            context.context.event.set_extra("cwa_owned", False)
            return json.dumps({"status": "error", "message": str(exc)[:1500]}, ensure_ascii=False)


def object_schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


class Main(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.runtime = None
        self.routes = []
        self.response_wrapper = None
        self.previous_responses = None
        self.tool_instances = []

    async def initialize(self):
        data = Path(get_astrbot_data_path()).resolve()
        roots = [data / "temp", data / "attachments", data / "agent" / "comfyui" / "input", data / "plugin_data" / "astrbot_plugin_comfyui"]
        legacy = Path(data.anchor) / "home/ubuntu/AstrBot/data/agent/comfyui/input"
        if legacy.is_dir():
            roots.append(legacy)
        self.runtime = Runtime(StarTools.get_data_dir(PLUGIN), self.send, roots)
        descriptions = {
            "comfyui_workflows": "列出绘图工作流、输入槽位、图片顺序和尺寸范围。查询过程不要发送进度话。",
            "comfyui_generate": "创建绘图任务。程序自动发送开始提示和最终图片；返回后结束绘图回复，不要读图、轮询或另外发图。用户给具体尺寸时准确传入，否则按用途选尺寸。",
            "comfyui_tasks": "查询当前会话中本人任务，区分生成和发送状态。无需反复查询等待。返回 media 引用可用于后续改图。",
            "comfyui_resend": "仅当用户明确要求重发时，重新发送原任务图片到原会话，不再生成。发送结果未确认的任务可能已送达，应先告诉用户这个状态。",
        }
        strings = {"type": "array", "items": {"type": "string"}}
        generate = object_schema({"workflow_name": {"type": "string", "description": "工作流名称或 ID"}, "texts": {**strings, "description": "按工作流文字槽位顺序填写本次描述或修改要求，程序自动添加已配置的前缀，无需重复填写"}, "image_urls": {**strings, "description": "按图片槽位排序的 URL、图片助手占位符、媒体引用或本地媒体路径；省略时使用当前/引用/最近消息的明确图片，不传原始 Base64"}, "width": {"type": "integer", "description": "宽度，与 height 成对；须符合工作流尺寸范围"}, "height": {"type": "integer", "description": "高度，与 width 成对"}, "caption": {"type": "string", "description": "随最终图片发送的一句简短配文，可沿用当前人格，不声称看过生成画面"}}, ["workflow_name", "texts"])
        schemas = {"comfyui_workflows": object_schema(), "comfyui_generate": generate, "comfyui_tasks": object_schema({"task_id": {"type": "string", "description": "省略时查询本人的最近任务"}}), "comfyui_resend": object_schema({"task_id": {"type": "string"}}, ["task_id"])}
        self.tool_instances = [AssistantTool(name=n, description=descriptions[n], parameters=schemas[n], plugin=self) for n in descriptions]
        self.context.add_llm_tools(*self.tool_instances)
        for route, method in (("state", "GET"), ("action", "POST")):
            handler = self.api(route)
            path = f"/{PLUGIN}/{route}"
            self.context.register_web_api(path, handler, [method], "ComfyUI assistant management")
            self.routes.append((path, handler))
        self.install_response_observer()
        await self.runtime.start()

    async def send(self, scope, text, paths):
        for path in paths:
            if not Path(path).is_file():
                raise Rejected("图片文件不存在")
        chain = MessageChain(chain=[*[Image.fromFileSystem(p) for p in paths], *([Plain(text)] if text else [])])
        try:
            return await self.context.send_message(scope, chain)
        except Exception as exc:
            if type(exc).__name__ in ("ActionFailed", "ApiNotAvailable"):
                raise Rejected(f"平台拒绝发送：{str(exc)[:500]}") from exc
            raise

    async def candidates(self, event, messages=()):
        def extract(components):
            result = []
            for component in components or []:
                kind = getattr(component, "type", "")
                if str(kind).lower().endswith("image") or isinstance(component, Image):
                    for value in (getattr(component, "url", None), getattr(component, "path", None), getattr(component, "file", None)):
                        if value and (str(value).startswith(("http://", "https://", "base64://", "file://", "data:image/")) or Path(str(value)).is_absolute()):
                            result.append(str(value))
                            break
            return result
        components = event.message_obj.message
        found = extract(components)
        if found:
            return found
        for component in components:
            found = extract(getattr(component, "chain", None))
            if found:
                return found
        # Media tools can attach images to this turn before host history is saved.
        for message in reversed(messages or []):
            role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
            if role != "user":
                continue
            content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
            found = []
            for part in content if isinstance(content, list) else []:
                image = part.get("image_url") if isinstance(part, dict) else getattr(part, "image_url", None)
                url = image.get("url") if isinstance(image, dict) else getattr(image, "url", None)
                if url:
                    found.append(url)
            if found:
                return found
            break
        manager = self.context.conversation_manager
        cid = await manager.get_curr_conversation_id(event.unified_msg_origin)
        if not cid:
            return []
        conversation = await manager.get_conversation(event.unified_msg_origin, cid)
        history = json.loads(conversation.history or "[]") if conversation else []
        checked = 0
        for message in reversed(history):
            if message.get("role") != "user":
                continue
            checked += 1
            content = message.get("content")
            if isinstance(content, list):
                found = [p["image_url"]["url"] for p in content if isinstance(p, dict) and p.get("type") == "image_url" and isinstance(p.get("image_url"), dict) and p["image_url"].get("url")]
                if found:
                    return found
            if checked >= 5:
                break
        return []

    async def dispatch(self, name, event, args, messages=()):
        rt = self.runtime
        scope, owner = event.unified_msg_origin, str(event.get_sender_id())
        if name == "comfyui_workflows":
            return {"workflows": rt.workflows(), "size_limits": {k: rt.settings[k] for k in ("min_edge", "max_edge", "multiple", "max_pixels")}, "instruction": GUIDANCE}
        if name == "comfyui_generate":
            candidates = []
            workflow = next((w for w in rt.store.workflows() if args.get("workflow_name") in (w["id"], w["name"])), None)
            needs_images = workflow and workflow["bindings"]["images"]
            if needs_images and (not args.get("image_urls") or any(s in ("base64://ASTRBOT_PLUGIN_CACHE_PENDING", "IMAGE_DATA_READY_INTERNAL") for s in args.get("image_urls", []))):
                candidates = await self.candidates(event, messages)
            result = await rt.create(scope, owner, str(event.message_obj.message_id), candidates=candidates, **args)
            event.set_extra("cwa_owned", True)
            return {"task": result, "instruction": "任务已持久化，程序会自动发图。结束本轮绘图回复，不轮询、不读图、不调用其他发送工具。"}
        if name == "comfyui_tasks":
            if args.get("task_id"):
                rt.refresh(args["task_id"], scope, owner)
            else:
                for task in rt.tasks(scope, owner):
                    rt.refresh(task["id"], scope, owner)
            return {"tasks": rt.tasks(scope, owner, args.get("task_id"))}
        if name == "comfyui_resend":
            result = await rt.resend(args["task_id"], scope, owner)
            event.set_extra("cwa_owned", result["delivery"] == "sent")
            return {"task": result, "instruction": "重发使用原图。若已发送成功，不再追加重复配文。"}
        raise ValueError("未知绘图工具")

    @filter.on_llm_request()
    async def instructions(self, event: AstrMessageEvent, req: ProviderRequest):
        req.system_prompt = (req.system_prompt or "") + "\n\n" + GUIDANCE + " 开始绘图流程时先调用 comfyui_workflows，再按需获取历史图片；这些准备步骤均不输出进度话。"

    @filter.on_agent_begin()
    async def agent_begin(self, event, context):
        event.set_extra("cwa_observe", True)
        for key in ("cwa_intermediate", "cwa_owned", "cwa_mixed", "cwa_preparing"):
            event.set_extra(key, False)

    def install_response_observer(self):
        # Host hooks run after intermediate text. Observe, but do not rewrite, responses.
        original = ToolLoopAgentRunner._iter_llm_responses
        self.previous_responses = original
        plugin = self

        async def observed(runner, *args, **kwargs):
            async for response in original(runner, *args, **kwargs):
                event = runner.run_context.context.event
                if plugin.runtime and not plugin.runtime.stopped and event.get_extra("cwa_observe", False) and not response.is_chunk:
                    names = set(response.tools_call_name or [])
                    if names & {"comfyui_workflows", "comfyui_generate"}:
                        event.set_extra("cwa_preparing", True)
                    permitted = TOOLS | MEDIA_TOOLS if event.get_extra("cwa_preparing", False) else TOOLS
                    event.set_extra("cwa_intermediate", bool(names) and names <= permitted)
                    if names and not names <= permitted:
                        event.set_extra("cwa_mixed", True)
                yield response
        self.response_wrapper = observed
        ToolLoopAgentRunner._iter_llm_responses = observed

    @filter.on_decorating_result()
    async def decorate(self, event):
        result = event.get_result()
        if not result:
            return
        if event.get_extra("cwa_intermediate", False) or (event.get_extra("cwa_owned", False) and not event.get_extra("cwa_mixed", False)):
            result.chain.clear()

    def api(self, route):
        async def handler():
            if not self.runtime or self.runtime.stopped:
                return error_response("绘图助手已停用", status_code=503)
            try:
                rt = self.runtime
                if route == "state":
                    return json_response({"version": __version__, "settings": rt.settings, "workflows": rt.store.workflows(), "tasks": rt.tasks(admin=True)})
                if len(await request.body()) > 20 * 1024 * 1024:
                    raise ValueError("上传内容超过 20 MB")
                data = await request.json(default={})
                if not isinstance(data, dict):
                    raise ValueError("请求内容必须是对象")
                action = data.get("action")
                if action == "save_workflow":
                    result = rt.save_workflow(data["workflow"])
                elif action == "delete_workflow":
                    rt.store.delete_workflow(data["id"])
                    result = {"deleted": True}
                elif action == "settings":
                    result = rt.configure(data["settings"])
                elif action == "refresh_task":
                    result = rt.refresh(data["id"])
                elif action == "resend":
                    result = await rt.resend(data["id"])
                elif action == "cleanup":
                    rt.cleanup()
                    result = {"cleaned": True}
                else:
                    raise ValueError("未知管理操作")
                return json_response(result)
            except Exception as exc:
                return error_response(str(exc)[:1500])
        return handler

    async def terminate(self):
        if self.runtime:
            await self.runtime.stop()
        if self.response_wrapper and ToolLoopAgentRunner._iter_llm_responses is self.response_wrapper:
            ToolLoopAgentRunner._iter_llm_responses = self.previous_responses
        for tool in self.tool_instances:
            items = self.context.provider_manager.llm_tools.func_list
            if tool in items:
                items.remove(tool)
        self.context.registered_web_apis[:] = [api for api in self.context.registered_web_apis if not any(api[0] == path and api[1] is handler for path, handler in self.routes)]
