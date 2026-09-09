"""Import legacy workflow documents without copying plugin code or old tasks."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from assistant.catalog import settings_checked, workflow_checked  # noqa: E402
from assistant.store import Store  # noqa: E402


def migrate(source, destination):
    store = Store(destination / "assistant.sqlite3")
    existing = {w["id"] for w in store.workflows()}
    meta_path = source / "workflow_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8-sig")) if meta_path.exists() else {}
    imported = []
    try:
        for p in sorted((source / "workflows").glob("Krea2*.json")):
            identifier = "legacy-" + hashlib.sha256(p.name.encode()).hexdigest()[:20]
            if identifier in existing:
                continue
            description = meta.get("descriptions", {}).get(p.name, {})
            if isinstance(description, str):
                description = {"detailed": description}
            w = workflow_checked({"id": identifier, "name": p.stem.split("+")[0], "graph": json.loads(p.read_text(encoding="utf-8-sig")), "short": description.get("short", ""), "detailed": description.get("detailed", "")})
            labels = meta.get("text_slots", {}).get(p.name, [])
            for index, row in enumerate(w["bindings"]["texts"]):
                if index < len(labels):
                    row["label"] = labels[index]
                elif len(w["bindings"]["texts"]) == 1:
                    row["label"] = ("绘画描述", "修改要求", "互动或合成要求")[min(len(w["bindings"]["images"]), 2)]
            store.save_workflow(w)
            imported.append({"id": identifier, "name": w["name"], "bindings": w["bindings"], "source_sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
        if not store.configuration():
            store.configure(settings_checked({}))
    finally:
        store.close()
    return imported


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(migrate(args.source.resolve(), args.data.resolve()), ensure_ascii=False, indent=2))
