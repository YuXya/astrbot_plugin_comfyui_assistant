"""Resolve bounded media inputs without exposing binary data to the model."""

import base64
import io
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
from PIL import Image


PLACEHOLDERS = {"base64://ASTRBOT_PLUGIN_CACHE_PENDING", "IMAGE_DATA_READY_INTERNAL"}
MAX_BYTES = 25 * 1024 * 1024


def image_checked(raw):
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("图片为空或超过 25 MB")
    with Image.open(io.BytesIO(raw)) as im:
        if im.format not in ("PNG", "JPEG", "WEBP", "GIF") or im.width * im.height > 40000000:
            raise ValueError("仅支持 PNG、JPEG、WebP、GIF，图片像素不能超过四千万")
        im.verify()
    return raw


def local_path(source):
    if source.startswith("file://"):
        parsed = urlsplit(source)
        if parsed.netloc not in ("", "localhost"):
            raise ValueError("不支持网络文件共享路径")
        source = unquote(parsed.path)
        if re.match(r"^/[A-Za-z]:", source):
            source = source[1:]
    return Path(source).resolve()


class Media:
    def __init__(self, roots, reference, authorize_local=None):
        self.roots = [Path(p).resolve() for p in roots]
        self.reference = reference
        self.authorize_local = authorize_local

    async def read(self, source, scope, owner):
        if not isinstance(source, str) or len(source) > MAX_BYTES * 2:
            raise ValueError("图片来源格式错误")
        if source.startswith("comfy-media:"):
            source = self.reference(source, scope, owner)
        if source.startswith(("data:image/", "base64://")):
            encoded = source.split(",", 1)[1] if source.startswith("data:") else source[9:]
            try:
                raw = base64.b64decode(encoded, validate=True)
            except Exception as exc:
                raise ValueError("图片 Base64 无效或占位符尚未解析") from exc
        elif source.startswith(("http://", "https://")):
            async with httpx.AsyncClient(timeout=30, follow_redirects=True, trust_env=False) as client:
                async with client.stream("GET", source) as response:
                    response.raise_for_status()
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ValueError("图片下载超过 25 MB")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
        else:
            p = local_path(source)
            if not any(p.is_relative_to(root) for root in self.roots) or not p.is_file():
                raise ValueError("图片文件不存在或不在允许的媒体目录中，请重新上传或引用图片")
            if self.authorize_local:
                self.authorize_local(p, scope, owner)
            if p.stat().st_size > MAX_BYTES:
                raise ValueError("图片超过 25 MB")
            raw = p.read_bytes()
        return image_checked(raw)

    async def resolve(self, sources, candidates, count, scope, owner):
        if not isinstance(sources, list) or any(not isinstance(s, str) for s in sources):
            raise ValueError("image_urls 必须是图片来源列表")
        if not sources and count:
            sources = list(candidates)
        if len(sources) != count:
            raise ValueError(f"需要明确提供 {count} 张图片，当前找到 {len(sources)} 张；请按图片槽位顺序指定")
        resolved = []
        for index, source in enumerate(sources):
            if source in PLACEHOLDERS:
                if len(candidates) != count:
                    raise ValueError("图片占位符没有对应图片，请上传或引用所需图片；不会重复使用第一张凑数")
                source = candidates[index]
            resolved.append(base64.b64encode(await self.read(source, scope, owner)).decode("ascii"))
        return resolved
