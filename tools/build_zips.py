#!/usr/bin/env python3
"""把仓库里的 6 个插件各打成一个 zip（AstrBot 插件市场 / 面板上传格式）。

zip 内部结构：<插件目录名>/main.py、metadata.yaml、_conf_schema.json ...
"""

import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "plugins"
OUT = REPO / "dist"
SKIP = {"__pycache__", ".pytest_cache", ".git"}

OUT.mkdir(exist_ok=True)
total = 0
for pdir in sorted(p for p in SRC.iterdir() if p.is_dir()):
    zpath = OUT / f"{pdir.name}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(pdir.rglob("*")):
            if not f.is_file():
                continue
            if any(part in SKIP for part in f.parts):
                continue
            arc = Path(pdir.name) / f.relative_to(pdir)
            z.write(f, arc.as_posix())
    n = len(zipfile.ZipFile(zpath).namelist())
    kb = zpath.stat().st_size / 1024
    total += zpath.stat().st_size
    print(f"{pdir.name:<42} {n:>2} 个文件  {kb:>7.1f} KB")

print(f"\n共 {len(list(OUT.glob('*.zip')))} 个 zip，合计 {total/1024:.1f} KB → {OUT}")
