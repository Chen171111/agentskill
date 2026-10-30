"""在干净子进程内验证 quant2 桌面适配层（合成行情，不读权威数据）。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop.service import BacktestConfig, run_stock_backtest


def synthetic_bars() -> pd.DataFrame:
    rows = []
    dates = pd.bdate_range("2024-01-01", periods=160)
    for n in range(100):
        code = f"SH{600000 + n:06d}"
        for i, date in enumerate(dates):
            close = 10.0 + (n % 7) * 0.001 * i + 0.03 * np.sin(i / 9 + n)
            rows.append({"code": code, "date": date.strftime("%Y%m%d"),
                         "open": close, "close": close, "high": close,
                         "low": close, "volume": 100000.0 + n * 100,
                         "amount": close * (100000.0 + n * 100) * 100})
    return pd.DataFrame(rows)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="quant2 桌面适配层合成行情自检")
    parser.add_argument("--project-root", required=True)
    args = parser.parse_args(argv)
    cfg = BacktestConfig(kind="stock", strategy="stock_multifactor",
                         start="20240501", end="20240830", topk=5, rebalance=5)
    result = run_stock_backtest(cfg, args.project_root, bars=synthetic_bars())
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
