#!/usr/bin/env python3
"""LAN 清屏探测入口：文档没有 hide API，默认走占位符 upgrade。

硬件 1–3（半角空格 / 全角空格 / \\n）确认可留待。本脚本只是 --file --pause 的包装。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "clear_probe.txt"


def main() -> None:
    cmd = [
        sys.executable,
        str(ROOT / "glasses" / "display_server.py"),
        "--file",
        str(FIXTURE),
        "--pause",
        *sys.argv[1:],
    ]
    raise SystemExit(subprocess.call(cmd))


if __name__ == "__main__":
    main()
