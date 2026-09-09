"""Durable generation and delivery, independent of the LLM turn lifetime."""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from pathlib import Path

from .backend import Backend, Rejected
from .catalog import build_graph, public_workflow, render_start_message, scaled_dimensions, settings_checked, workflow_checked
from .media import Media, image_checked
from .store import Store


log = logging.getLogger(__name__)
ACTIVE = {"queued", "submitting", "submission_unknown", "running"}


class Runtime:
    def __init__(self, directory, sender, roots=(), backend=None):
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.root / "assistant.sqlite3")
        self.settings = settings_checked(self.store.configuration())
        self.sender = sender
        self.backend = backend or Backend()
        self.media = Media([self.root, *roots], self.media_reference, self.authorize_local_media)
        self.workers = {}
        self.locks = {}
        self.create_lock = asyncio.Lock()
        self.stopped = False
        self.housekeeping = None

    async def start(self):
        for job in self.store.tasks():
            changed = False
            for phase in ("start", "delivery", "notice"):
                if job.get(phase) == "sending":
                    job[phase] = "unknown"
                    for attempt in job["attempts"]:
                        if attempt["phase"] == phase and attempt["status"] == "sending":
                            attempt.update(status="unknown", error="插件重启，发送结果未确认")
                    changed = True
            if job["generation"] == "submitting":
                job["generation"] = "submission_unknown"
                changed = True
            if changed:
                self.store.save_task(job)
            if job["generation"] in ACTIVE or (job["generation"] == "completed" and job["delivery"] == "pending"):
                self.spawn(job["id"])
        self.cleanup()
        self.housekeeping = asyncio.create_task(self._housekeeping())

    def save_workflow(self, data):
        w = workflow_checked(data)
        self.store.save_workflow(w)
        return w

    def configure(self, data):
        self.settings = settings_checked({**self.settings, **data})
        self.store.configure(self.settings)
        return self.settings

    def workflows(self):
        return [public_workflow(w) for w in self.store.workflows() if w["enabled"]]

    def _owned(self, task_id, scope=None, owner=None):
        job = self.store.task(task_id)
        if scope is not None and (job["scope"] != scope or job["owner"] != owner):
            raise ValueError("当前会话中没有你提交的这个任务")
        return job

    def media_reference(self, source, scope, owner):
        parts = source.split(":")
        if len(parts) != 3:
            raise ValueError("图片引用格式错误")
        job = self._owned(parts[1], scope, owner)
        try:
            index = int(parts[2])
            if index < 0:
                raise ValueError()
            p = self.root / job["outputs"][index]
        except (ValueError, IndexError) as exc:
            raise ValueError("图片引用不存在") from exc
        if job.get("files_expired") or not p.is_file():
            raise ValueError("图片已过期，请重新提供图片")
        return str(p)

    def authorize_local_media(self, path, scope, owner):
        task_root = self.root / "tasks"
        if path.is_relative_to(task_root):
            parts = path.relative_to(task_root).parts
            if len(parts) != 2:
                raise ValueError("图片任务路径无效")
            job = self._owned(parts[0], scope, owner)
            if path.relative_to(self.root).as_posix() not in job["outputs"] or job.get("files_expired"):
                raise ValueError("仅可使用本人的有效任务输出图片")

    def public_task(self, job, admin=False):
        keys = ("id", "workflow_name", "generation", "delivery", "start", "error", "created", "updated", "width", "height", "input_width", "input_height", "files_expired", "prompt_id")
        result = {k: job.get(k) for k in keys}
        result["media"] = [] if job.get("files_expired") else [f"comfy-media:{job['id']}:{i}" for i in range(len(job["outputs"]))]
        result["delivery_meaning"] = "sent 表示平台发送调用返回成功，不表示用户已经看到图片"
        if admin:
            result.update(scope=job["scope"], owner=job["owner"], attempts=job["attempts"])
        return result

    def tasks(self, scope=None, owner=None, task_id=None, admin=False):
        jobs = [self._owned(task_id, scope, owner)] if task_id else self.store.tasks()
        return [self.public_task(j, admin) for j in jobs if scope is None or (j["scope"] == scope and j["owner"] == owner)][:100]

    async def create(self, scope, owner, event_id, workflow_name, texts=None, image_urls=None, width=None, height=None, caption="", candidates=()):
        if self.stopped:
            raise ValueError("绘图助手已停用")
        if not scope or not owner or not event_id:
            raise ValueError("无法确认当前会话和请求来源")
        signature = json.dumps([scope, owner, event_id, workflow_name, texts, image_urls, width, height], ensure_ascii=False, sort_keys=True)
        event_key = hashlib.sha256(signature.encode()).hexdigest()
        async with self.create_lock:
            previous = self.store.by_event(event_key)
            if previous:
                return self.public_task(previous)
            w = next((w for w in self.store.workflows() if workflow_name in (w["id"], w["name"]) and w["enabled"]), None)
            if not w:
                raise ValueError("工作流不存在或已停用，请先查询可用工作流")
            input_width, input_height = scaled_dimensions(width, height, self.settings, w["bindings"], w.get("size_scale", 1))
            start_message = render_start_message(self.settings["start_message_template"], width, height, w["name"])
            images = await self.media.resolve(image_urls or [], candidates, len(w["bindings"]["images"]), scope, owner)
            graph = build_graph(w, texts or [], images, width, height, self.settings)
            identifier = uuid.uuid4().hex
            folder = self.root / "tasks" / identifier
            folder.mkdir(parents=True)
            (folder / "request.json").write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
            job = {"id": identifier, "event_key": event_key, "scope": scope, "owner": owner, "created": time.time(), "workflow_name": w["name"], "workflow_snapshot": w, "server": self.settings["server_url"], "generation": "queued", "delivery": "pending", "start": "pending", "notice": "pending", "prompt_id": None, "outputs": [], "attempts": [], "error": "", "width": width, "height": height, "input_width": input_width, "input_height": input_height, "start_message": start_message, "caption": str(caption or "画好了。")[:500], "files_expired": False}
            self.store.save_task(job)
            self.spawn(identifier)
            return self.public_task(job)

    def spawn(self, task_id):
        if self.stopped or task_id in self.workers:
            return
        task = asyncio.create_task(self._run(task_id))
        self.workers[task_id] = task
        task.add_done_callback(lambda completed: self.workers.pop(task_id, None))

    async def _housekeeping(self):
        while True:
            await asyncio.sleep(3600)
            self.cleanup()

    async def _send(self, job, phase, text, paths):
        attempt = {"id": uuid.uuid4().hex, "phase": phase, "at": time.time(), "status": "sending", "error": ""}
        job[phase] = "sending"
        job["attempts"].append(attempt)
        self.store.save_task(job)
        try:
            accepted = await asyncio.wait_for(self.sender(job["scope"], text, paths), timeout=60)
            if accepted is not True:
                raise Rejected("宿主未确认发送成功")
            attempt["status"] = "sent"
            job[phase] = "sent"
        except Rejected as exc:
            attempt.update(status="failed", error=str(exc)[:1000])
            job[phase] = "failed"
        except asyncio.CancelledError:
            attempt.update(status="unknown", error="发送被中断，结果未确认")
            job[phase] = "unknown"
            self.store.save_task(job)
            raise
        except Exception as exc:
            attempt.update(status="unknown", error=f"{type(exc).__name__}: {str(exc)[:500]}")
            job[phase] = "unknown"
        attempt["finished"] = time.time()
        if phase == "delivery":
            job["error"] = attempt["error"]
        self.store.save_task(job)

    async def _deliver(self, job):
        if job["delivery"] != "pending":
            return
        paths = [str(self.root / p) for p in job["outputs"]]
        if not paths or any(not Path(p).is_file() for p in paths):
            job.update(delivery="failed", error="生成图片文件缺失，不能发送；不会自动重新生成")
            self.store.save_task(job)
            return
        await self._send(job, "delivery", job["caption"], paths)
        if job["delivery"] == "failed" and job["notice"] == "pending":
            await self._send(job, "notice", "图片已生成，但发送未成功。可以查询任务原因或要求重发原图。", [])

    async def _run(self, task_id):
        async with self.locks.setdefault(task_id, asyncio.Lock()):
            job = self.store.task(task_id)
            try:
                if job["start"] == "pending":
                    size = f"（{job['width']}×{job['height']}）" if job["width"] else ""
                    await self._send(job, "start", job.get("start_message", f"开始绘制{size}，完成后发给你。"), [])
                if job["generation"] == "queued":
                    graph = json.loads((self.root / "tasks" / task_id / "request.json").read_text(encoding="utf-8"))
                    job["generation"] = "submitting"
                    self.store.save_task(job)
                    try:
                        job["prompt_id"] = await self.backend.submit(job["server"], graph, task_id)
                        job["generation"] = "running"
                    except Rejected:
                        raise
                    except Exception as exc:
                        job.update(generation="submission_unknown", error=f"提交结果未确认：{type(exc).__name__}，不会重复提交")
                    self.store.save_task(job)
                if job["generation"] == "submission_unknown":
                    found = await self.backend.find_submission(job["server"], task_id)
                    if not found:
                        job["error"] = "提交结果未确认，服务器暂未找到原任务。可稍后刷新状态；不会重新提交。"
                        self.store.save_task(job)
                        return
                    job.update(prompt_id=found, generation="running", error="")
                    self.store.save_task(job)
                deadline = time.monotonic() + self.settings["tracking_minutes"] * 60
                while job["generation"] == "running" and not self.stopped:
                    if time.monotonic() > deadline:
                        job.update(generation="tracking_paused", error="跟踪暂时超时，查询可继续核对原任务；不会重新生成")
                        self.store.save_task(job)
                        return
                    try:
                        result = await self.backend.history(job["server"], job["prompt_id"])
                    except Exception as exc:
                        job["error"] = f"查询暂时失败：{type(exc).__name__}，将继续查询原任务"
                        self.store.save_task(job)
                        await asyncio.sleep(self.settings["poll_seconds"])
                        continue
                    if result and result.get("status", {}).get("status_str") == "error":
                        detail = next((m.get("exception_message", "工作流执行失败") for kind, m in result.get("status", {}).get("messages", []) if kind == "execution_error"), "工作流执行失败")
                        raise Rejected(str(detail)[:1200])
                    if result and result.get("status", {}).get("completed"):
                        descriptors = []
                        for nid in job["workflow_snapshot"]["bindings"]["outputs"]:
                            descriptors.extend(result.get("outputs", {}).get(nid, {}).get("images", []))
                        if not descriptors:
                            raise Rejected("工作流执行完成，但所选输出节点没有图片")
                        files = []
                        for index, descriptor in enumerate(descriptors):
                            raw = image_checked(await self.backend.download(job["server"], descriptor))
                            rel = f"tasks/{task_id}/output_{index}.png"
                            p = self.root / rel
                            p.with_suffix(".partial").write_bytes(raw)
                            p.with_suffix(".partial").replace(p)
                            files.append(rel)
                        job.update(generation="completed", outputs=files, error="", generated_at=time.time())
                        self.store.save_task(job)
                        break
                    await asyncio.sleep(self.settings["poll_seconds"])
                if job["generation"] == "completed":
                    await self._deliver(job)
            except asyncio.CancelledError:
                raise
            except Rejected as exc:
                job.update(generation="failed", error=str(exc)[:1200])
                self.store.save_task(job)
                if job["notice"] == "pending":
                    await self._send(job, "notice", "这次绘图未完成：" + job["error"], [])
            except Exception as exc:
                log.warning("Task %s paused: %s", task_id, type(exc).__name__)
                job["error"] = f"处理暂时中断：{type(exc).__name__}: {str(exc)[:500]}。查询可继续核对原任务。"
                if job["generation"] == "submitting":
                    job["generation"] = "submission_unknown"
                self.store.save_task(job)

    def refresh(self, task_id, scope=None, owner=None):
        job = self._owned(task_id, scope, owner)
        if job["generation"] == "tracking_paused":
            job["generation"] = "running"
            self.store.save_task(job)
        if job["generation"] in ACTIVE or (job["generation"] == "completed" and job["delivery"] == "pending"):
            self.spawn(task_id)
        return self.public_task(job)

    async def resend(self, task_id, scope=None, owner=None):
        if self.stopped:
            raise ValueError("绘图助手已停用")
        job = self._owned(task_id, scope, owner)
        if task_id in self.workers or (task_id in self.locks and self.locks[task_id].locked()):
            return self.public_task(job)
        async with self.locks.setdefault(task_id, asyncio.Lock()):
            job = self._owned(task_id, scope, owner)
            if job["generation"] != "completed":
                raise ValueError("该任务尚无可重发的生成结果，请先查询状态")
            if job.get("files_expired") or not job["outputs"] or any(not (self.root / p).is_file() for p in job["outputs"]):
                raise ValueError("原图已过期或缺失，不能重发；不会自动重新生成")
            job["delivery"] = "pending"
            self.store.save_task(job)
            await self._deliver(job)
            return self.public_task(job)

    def cleanup(self):
        now = time.time()
        for job in self.store.tasks():
            if job["id"] in self.workers or job["generation"] in ACTIVE or job["generation"] == "tracking_paused":
                continue
            age = now - job.get("generated_at", job["created"])
            folder = (self.root / "tasks" / job["id"]).resolve()
            if not folder.is_relative_to(self.root / "tasks"):
                continue
            if age > self.settings["image_days"] * 86400 and not job.get("files_expired"):
                if folder.is_dir():
                    for p in folder.iterdir():
                        if p.is_file():
                            p.unlink()
                job["files_expired"] = True
                self.store.save_task(job)
            if age > self.settings["record_days"] * 86400:
                if folder.is_dir() and not any(folder.iterdir()):
                    folder.rmdir()
                self.store.delete_task(job["id"])

    async def stop(self):
        self.stopped = True
        pending = list(self.workers.values())
        if self.housekeeping:
            pending.append(self.housekeeping)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await self.backend.close()
        self.store.close()
