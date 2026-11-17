"""Re-run documented results from cached quotes, without downloads or ledger writes."""
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import config
import pipeline
import numpy as np
from backtest.account import BacktestAccount
from dataprovider.store import DataStore


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    pools = ("ETF全球", "ETF宽基", "ETF行业", "ETF稳健")
    store = DataStore()
    paths = sorted({store.path_for(c) for p in pools for c in config.RECOMMENDED_POOLS[p]})
    if not all(p.is_file() for p in paths):
        raise RuntimeError("Missing cached input: downloads disabled")
    before = {str(p.relative_to(ROOT)): digest(p) for p in paths}
    original_init = DataStore.__init__
    results = []
    minimum_cash = []
    original_mark = BacktestAccount.mark_to_close
    def checked_mark(self, date, prices):
        assert np.isfinite(self.cash) and self.cash >= -1e-8, "invalid backtest cash"
        minimum_cash.append(float(self.cash))
        return original_mark(self, date, prices)
    jobs = [(p, "etf_rotation", "20190101", "20260911") for p in pools]
    jobs += [("ETF全球", s, a, b)
             for s in ("etf_rotation", "etf_rotation_multi")
             for a, b in (("20190101", "20221231"), ("20230101", "20260911"))]
    jobs += [("ETF全球", "etf_rotation_multi", "20190101", "20260911")]
    with tempfile.TemporaryDirectory() as tmp:
        def isolated_init(self, *a, **kw):
            original_init(self, *a, **kw)
            self.split_log = Path(tmp) / "split_repairs.log"
        with patch.object(DataStore, "__init__", isolated_init), \
             patch.object(DataStore, "ensure", return_value=[]), \
             patch.object(BacktestAccount, "mark_to_close", checked_mark):
            for pool, strategy, start, end in jobs:
                run = pipeline.run_backtest(config.RECOMMENDED_POOLS[pool], strategy=strategy,
                                            topk=5, rebalance=5, start=start, end=end)
                eq = run["result"].equity
                assert np.isfinite(eq.to_numpy()).all(), "non-finite backtest output"
                row = {"pool": pool, "strategy": strategy, "requested_start": start,
                       "requested_end": end, "meta": run["meta"], "days": len(eq),
                       "metrics": run["metrics"]}
                results.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
    after = {str(p.relative_to(ROOT)): digest(p) for p in paths}
    assert after == before, "cached inputs changed"
    output = {"source": "local CSV cache; no refresh", "inputs": before,
              "unchanged_inputs": len(paths), "completed_runs": len(results), "cash_observations": len(minimum_cash),
              "minimum_virtual_cash": min(minimum_cash),
              "split_ratios": "jump-inferred unless CSV supplies explicit split_ratio; approximate",
              "results": results}
    Path(__file__).with_name("repaired_backtests.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
