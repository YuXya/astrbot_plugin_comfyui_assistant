"""ComfyUI transport with stable client-side submission identities."""

import httpx


class Rejected(Exception):
    """An operation is known not to have been accepted."""


class Backend:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=8), trust_env=False)

    async def submit(self, server, graph, identifier):
        response = await self.client.post(server + "/prompt", json={"prompt": graph, "client_id": "cwa-" + identifier, "extra_data": {"comfyui_assistant_task": identifier}})
        if 400 <= response.status_code < 500:
            raise Rejected("ComfyUI 拒绝任务：" + response.text[:1200])
        response.raise_for_status()
        data = response.json()
        if not data.get("prompt_id"):
            raise RuntimeError("ComfyUI 未返回任务编号，提交结果未确认")
        return data["prompt_id"]

    async def history(self, server, prompt_id):
        response = await self.client.get(server + "/history/" + prompt_id)
        response.raise_for_status()
        return response.json().get(prompt_id)

    async def find_submission(self, server, identifier):
        response = await self.client.get(server + "/queue")
        response.raise_for_status()
        queue = response.json()
        for item in queue.get("queue_running", []) + queue.get("queue_pending", []):
            if len(item) > 3 and item[3].get("comfyui_assistant_task") == identifier:
                return item[1]
        response = await self.client.get(server + "/history", params={"max_items": 200})
        response.raise_for_status()
        for key, item in response.json().items():
            prompt = item.get("prompt", [])
            if len(prompt) > 3 and prompt[3].get("comfyui_assistant_task") == identifier:
                return key
        return None

    async def download(self, server, descriptor):
        params = {k: descriptor.get(k, "") for k in ("filename", "subfolder", "type")}
        if params["type"] not in ("output", "temp") or not params["filename"]:
            raise ValueError("ComfyUI 输出描述无效")
        response = await self.client.get(server + "/view", params=params)
        response.raise_for_status()
        if len(response.content) > 50 * 1024 * 1024:
            raise ValueError("单张输出图片超过 50 MB")
        return response.content

    async def close(self):
        await self.client.aclose()
