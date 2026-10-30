"""Quant Workbench 启动入口。

双击 run_desktop.bat，或执行：
    python quant_desktop.py
    python quant_desktop.py --selftest
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Quant Workbench · A 股策略研究台")
    parser.add_argument("--selftest", action="store_true", help="无界面运行桌面端与策略自检")
    args = parser.parse_args(argv)
    if args.selftest:
        from tools.test_quant_desktop import run_selftest
        return run_selftest()
    from desktop.app import main as run_app
    run_app()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
