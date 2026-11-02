"""Prepare the static UI directory consumed by Tauri."""

import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend-dist"
FRONTEND.mkdir(exist_ok=True)

for asset in ("index.html", "favicon.svg"):
    shutil.copy2(ROOT / asset, FRONTEND / asset)
