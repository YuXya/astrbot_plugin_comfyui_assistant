"""Validated workflow catalog and per-request graph construction."""

import copy
import secrets
import uuid


DEFAULTS = {
    "server_url": "http://127.0.0.1:8188",
    "min_edge": 256,
    "max_edge": 2048,
    "multiple": 8,
    "max_pixels": 2097152,
    "record_days": 30,
    "image_days": 7,
    "poll_seconds": 3,
    "tracking_minutes": 30,
}


def settings_checked(data):
    from urllib.parse import urlsplit

    result = {**DEFAULTS, **data}
    url = str(result["server_url"]).strip().rstrip("/")
    if "://" not in url:
        url = "http://" + url
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("请填写有效的 ComfyUI HTTP/HTTPS 服务地址")
    result["server_url"] = url
    for key, low, high in (("min_edge", 8, 8192), ("max_edge", 8, 16384), ("multiple", 8, 64), ("max_pixels", 65536, 67108864), ("record_days", 1, 365), ("image_days", 1, 365), ("poll_seconds", 1, 60), ("tracking_minutes", 1, 1440)):
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{key} 需要是 {low}—{high} 的整数")
    if result["min_edge"] > result["max_edge"] or result["image_days"] > result["record_days"] or result["multiple"] % 8:
        raise ValueError("尺寸范围或保留天数不一致；尺寸步长必须是 8 的倍数")
    return {k: result[k] for k in DEFAULTS}


def graph_checked(graph):
    if not isinstance(graph, dict) or not graph or "nodes" in graph or len(graph) > 5000:
        raise ValueError("请上传 ComfyUI 导出的 API 格式 JSON 工作流")
    for key, node in graph.items():
        if not isinstance(key, str) or not isinstance(node, dict) or not isinstance(node.get("class_type"), str) or not isinstance(node.get("inputs"), dict):
            raise ValueError("工作流节点结构不完整")
        for value in node["inputs"].values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], int):
                if value[0] not in graph:
                    raise ValueError(f"节点 {key} 连接到不存在的节点 {value[0]}")
    return graph


def discover(graph):
    graph_checked(graph)
    binding = {"texts": [], "images": [], "width": [], "height": [], "outputs": []}
    for key, node in graph.items():
        kind, inputs = node["class_type"], node["inputs"]
        if kind == "Simple String":
            field = next((f for f in ("text", "string") if f in inputs), None)
            if field:
                binding["texts"].append({"node": key, "input": field, "label": "绘画描述 / 修改要求"})
        if kind == "ETN_LoadImageBase64" and "image" in inputs:
            binding["images"].append({"node": key, "input": "image", "label": f"图片{len(binding['images']) + 1}"})
        if kind in ("EmptyLatentImage", "EmptySD3LatentImage"):
            for field in ("width", "height"):
                if field in inputs:
                    binding[field].append({"node": key, "input": field})
        if kind == "SaveImage":
            binding["outputs"].append(key)
    return binding


def workflow_checked(workflow):
    w = copy.deepcopy(workflow)
    graph = graph_checked(w.get("graph"))
    w["id"] = str(w.get("id") or uuid.uuid4().hex)
    w["name"] = str(w.get("name", "")).strip()
    if not w["name"] or len(w["name"]) > 100:
        raise ValueError("工作流名称需要 1—100 个字符")
    w["enabled"] = bool(w.get("enabled", True))
    for field in ("short", "detailed"):
        w[field] = str(w.get(field, ""))[:10000]
    bindings = w.setdefault("bindings", discover(graph))
    seen = set()
    for kind in ("texts", "images", "width", "height"):
        rows = bindings.get(kind, [])
        if not isinstance(rows, list):
            raise ValueError("输入绑定必须为列表")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("输入绑定格式错误")
            key, field = str(row.get("node", "")), str(row.get("input", ""))
            if key not in graph or field not in graph[key]["inputs"] or (key, field) in seen:
                raise ValueError(f"无效或重复的绑定：{key}.{field}")
            seen.add((key, field))
            row.update(node=key, input=field)
        bindings[kind] = rows
    prefix = w.get("prompt_prefix", "")
    if not isinstance(prefix, str) or len(prefix) > 20000:
        raise ValueError("默认提示词前缀必须是文字，最多 20000 个字符")
    w["prompt_prefix"] = prefix
    target = w.get("prefix_target")
    if target is None and bindings["texts"]:
        first = bindings["texts"][0]
        target = {"node": first["node"], "input": first["input"]}
    if not bindings["texts"]:
        if prefix.strip():
            raise ValueError("设置默认提示词前缀前，请先绑定文字入口")
        target = None
    elif not isinstance(target, dict) or not any(target.get("node") == r["node"] and target.get("input") == r["input"] for r in bindings["texts"]):
        raise ValueError("前缀应用位置已失效，请重新选择文字入口")
    w["prefix_target"] = {"node": target["node"], "input": target["input"]} if target else None
    if bool(bindings["width"]) != bool(bindings["height"]):
        raise ValueError("宽度与高度入口必须同时绑定")
    outputs = bindings.get("outputs", [])
    if not isinstance(outputs, list) or not outputs or len(set(outputs)) != len(outputs) or any(n not in graph for n in outputs):
        raise ValueError("请选择至少一个有效且不重复的最终图片输出节点")
    return w


def dimensions(width, height, settings, bindings):
    if width is None and height is None:
        return
    if not bindings.get("width") or not bindings.get("height"):
        raise ValueError("该工作流尚未绑定尺寸入口，请省略宽高或先在管理页绑定")
    if any(isinstance(n, bool) or not isinstance(n, int) for n in (width, height)):
        raise ValueError("宽度和高度必须成对填写整数")
    if any(not settings["min_edge"] <= n <= settings["max_edge"] or n % settings["multiple"] for n in (width, height)) or width * height > settings["max_pixels"]:
        raise ValueError(f"每边须为 {settings['min_edge']}—{settings['max_edge']}、{settings['multiple']} 的倍数，总像素不超过 {settings['max_pixels']}；收到 {width}×{height}")


def build_graph(workflow, texts, images, width, height, settings):
    w = workflow_checked(workflow)
    b = w["bindings"]
    dimensions(width, height, settings, b)
    if not isinstance(texts, list) or len(texts) != len(b["texts"]) or any(not isinstance(t, str) or len(t) > 20000 for t in texts):
        raise ValueError(f"该工作流需要 {len(b['texts'])} 段文字，请按槽位顺序填写")
    if len(images) != len(b["images"]):
        raise ValueError(f"该工作流需要 {len(b['images'])} 张图片")
    graph = copy.deepcopy(w["graph"])
    for kind, values in (("texts", texts), ("images", images)):
        for row, value in zip(b[kind], values):
            if kind == "texts" and w["prompt_prefix"].strip() and w["prefix_target"] == {"node": row["node"], "input": row["input"]}:
                value = w["prompt_prefix"].rstrip("\r\n") + "\n" + value
                if len(value) > 20000:
                    raise ValueError("默认前缀与本次描述合计不能超过 20000 个字符，请缩短后重试")
            graph[row["node"]]["inputs"][row["input"]] = value
    for kind, value in (("width", width), ("height", height)):
        if value is not None:
            for row in b[kind]:
                graph[row["node"]]["inputs"][row["input"]] = value
    for node in graph.values():
        for key, value in node["inputs"].items():
            if key in ("seed", "noise_seed") and isinstance(value, int) and not isinstance(value, bool):
                node["inputs"][key] = secrets.randbelow(2**63)
    return graph


def public_workflow(w):
    return {"id": w["id"], "name": w["name"], "short": w["short"], "detailed": w["detailed"], "enabled": w["enabled"], "text_slots": [r.get("label", "文字") for r in w["bindings"]["texts"]], "image_slots": [r.get("label", "图片") for r in w["bindings"]["images"]], "custom_size": bool(w["bindings"]["width"])}
