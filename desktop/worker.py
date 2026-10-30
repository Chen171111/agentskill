"""独立回测子进程。结果写临时 JSON，stdout 仅用于桌面日志。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

from desktop.service import BacktestConfig, execute_backtest


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, allow_nan=False)
    os.replace(tmp, path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Quant Workbench 回测子进程")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    output = Path(args.output)
    try:
        cfg = BacktestConfig.from_dict(json.loads(args.config))
        result = execute_backtest(cfg, args.project_root)
        _write(output, {"ok": True, "result": result})
        print(f"\n[worker] 回测完成：{result['engine']}")
        return 0
    except Exception as exc:  # noqa: BLE001 - 子进程边界必须把任意引擎错误结构化返回
        _write(output, {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                        "traceback": traceback.format_exc()})
        print(f"[worker] 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
