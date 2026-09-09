"""Build a source-only plugin archive."""

import hashlib
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
output = root / "dist" / "astrbot_plugin_comfyui_assistant-0.1.1.zip"
output.parent.mkdir(exist_ok=True)
files = [root / f for f in ("main.py", "metadata.yaml", "requirements.txt", "README.md", "CHANGELOG.md")]
files += list((root / "assistant").glob("*.py"))
files += [p for p in (root / "pages").rglob("*") if p.is_file()]
files += [root / "scripts/migrate.py", root / "docs/validation.md"]
with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
    for p in files:
        archive.write(p, p.relative_to(root))
with zipfile.ZipFile(output) as archive:
    assert archive.testzip() is None
print(output)
print(hashlib.sha256(output.read_bytes()).hexdigest())
