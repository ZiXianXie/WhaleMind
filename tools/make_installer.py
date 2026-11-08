"""把 plugins/ 与本目录的安装脚本打成一个可分发的安装包。

用法：
    python tools/make_installer.py

产物：
    dist/whale-mind-0.1.0-installer.zip
"""

from __future__ import annotations

import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DIST = REPO / "dist"
VERSION = "0.1.0"
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".git", ".idea", ".vscode"}

EXTRA_FILES = [
    ("tools/install/install.ps1", "install.ps1"),
    ("tools/install/安装WhaleMind.bat", "安装WhaleMind.bat"),
    ("tools/install/使用说明.txt", "使用说明.txt"),
]


def iter_plugin_files():
    plugins = REPO / "plugins"
    for path in sorted(plugins.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(REPO)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        yield path, f"plugins/{path.relative_to(plugins).as_posix()}"


def main() -> int:
    DIST.mkdir(exist_ok=True)
    out = DIST / f"whale-mind-{VERSION}-installer.zip"

    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for src, arc in iter_plugin_files():
            zf.write(src, arc)
            count += 1
        for src_rel, arc in EXTRA_FILES:
            src = REPO / src_rel
            if not src.exists():
                print(f"[skip] 缺少 {src_rel}")
                continue
            zf.write(src, arc)
            count += 1

    size = out.stat().st_size
    print(f"已生成 {out.relative_to(REPO)}  ({count} 个文件, {size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
