"""Run neutral local workflows with a recording sender; never send QQ messages."""

import asyncio
import argparse
import hashlib
import io
import json
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from assistant.catalog import DEFAULTS, build_graph  # noqa: E402
from assistant.runtime import Runtime  # noqa: E402
from scripts.migrate import migrate  # noqa: E402


async def run(source, remaining=False):
    directory = ROOT / "validation" / ("local-" + time.strftime("%Y%m%d-%H%M%S"))
    directory.mkdir(parents=True)
    imported = migrate(source.resolve(), directory)
    sender_calls = []
    async def sender(scope, text, paths):
        sender_calls.append({"scope": scope, "image_count": len(paths)})
        return True
    rt = Runtime(directory, sender)
    refs = []
    for index, color in enumerate(("#cf443e", "#4468c9")):
        image = Image.new("RGB", (512, 512), "white")
        draw = ImageDraw.Draw(image)
        (draw.ellipse if index == 0 else draw.rectangle)((140, 140, 372, 372), fill=color)
        p = directory / f"reference-{index}.png"
        image.save(p)
        refs.append(str(p))
    checks = []
    try:
        for w in [w for w in rt.store.workflows() for _ in range(2 if remaining else 1)]:
            count = len(w["bindings"]["images"])
            texts = ["A small ceramic teapot and a green houseplant on a wooden table, natural light, clean product photograph, no text, no people.", "Change the background to pale blue. Preserve the red circle, its position and its shape. Flat graphic illustration, no text.", "Combine image one and image two into one simple graphic: the red circle on the left and the blue square on the right, white background, no text."]
            width, height = ((768, 1024), (1024, 768), (1024, 768))[count]
            if remaining:
                width, height = ((None, None) if len(checks) % 2 == 0 else ((1024, 768) if count == 0 else (768, 1024)))
            original = json.dumps(w["graph"], sort_keys=True)
            default_graph = build_graph(w, ["neutral test"], ["fixture"] * count, None, None, DEFAULTS)
            for axis in ("width", "height"):
                for binding in w["bindings"][axis]:
                    assert default_graph[binding["node"]]["inputs"][binding["input"]] == w["graph"][binding["node"]]["inputs"][binding["input"]]
            start = time.monotonic()
            result = await rt.create("test:FriendMessage:local", "fixture", str(len(checks)), w["name"], [texts[count]], refs[:count], width, height, "本地测试")
            while rt.workers:
                await asyncio.gather(*list(rt.workers.values()))
                await asyncio.sleep(0)
            job = rt.store.task(result["id"])
            if job["generation"] != "completed" or job["delivery"] != "sent":
                raise AssertionError(json.dumps(rt.public_task(job), ensure_ascii=False))
            sizes = []
            for rel in job["outputs"]:
                raw = (directory / rel).read_bytes()
                with Image.open(io.BytesIO(raw)) as im:
                    sizes.append(list(im.size))
                    if width is not None:
                        assert im.size == (width, height)
                    else:
                        assert im.width > 0 and im.height > 0
            assert json.dumps(w["graph"], sort_keys=True) == original
            checks.append({"workflow": w["name"], "reference_count": count, "requested_size": [width, height], "size": sizes, "generation": job["generation"], "recording_sender": job["delivery"], "prompt_id": job["prompt_id"], "seconds": round(time.monotonic() - start, 2), "template_sha256": hashlib.sha256(original.encode()).hexdigest()})
            print(json.dumps(checks[-1], ensure_ascii=False), flush=True)
        assert len(sender_calls) == 2 * len(checks)
        report = {"checks": checks, "imported": imported, "sender_calls": sender_calls, "qq_sent": False, "images_visually_inspected": False}
        (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("REPORT", directory / "report.json", flush=True)
    finally:
        await rt.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Legacy plugin data directory containing workflows/")
    parser.add_argument("--remaining", action="store_true", help="Validate default sizes and the other orientation")
    args = parser.parse_args()
    asyncio.run(run(args.source, args.remaining))
