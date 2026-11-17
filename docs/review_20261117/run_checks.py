"""Capture baseline tests and paired backtests without touching live ledgers."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def fingerprints():
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(ROOT.rglob("*.py"))
            if p.relative_to(ROOT).parts[0] not in (".venv", "docs", "state")}


def selftests():
    results = {}
    for label, force_utf8 in (("native_child_encoding", False), ("utf8_child_encoding", True)):
        env = os.environ.copy()
        if force_utf8:
            env["PYTHONUTF8"] = "1"
        proc = subprocess.run([sys.executable, "-B", "-X", "utf8", "tools/selftest.py"],
                              cwd=ROOT, env=env, capture_output=True,
                              encoding="utf-8", errors="replace", timeout=120)
        logfile = OUT / ("selftest_" + label + ".log")
        logfile.write_text(proc.stdout + proc.stderr, encoding="utf-8")
        results[label] = {"exit_code": proc.returncode, "log": str(logfile.relative_to(ROOT))}
    return results


def paired_backtests():
    import config
    from dataprovider.store import DataStore
    from pipeline import run_backtest
    from strategies.builtin import EtfRotationStrategy
    codes = config.RECOMMENDED_POOLS["ETF全球"]
    store = DataStore()
    missing = [c for c in codes if not store.path_for(c).exists()]
    assert not missing, "offline-only: missing cache " + repr(missing)
    store.ensure = lambda cs: None
    original = EtfRotationStrategy.generate_weights

    def apply_live_eligibility(self, date, factors, panel):
        weights = original(self, date, factors, panel)
        if weights is None:
            return None
        mom = factors["momentum20"].loc[date]
        return {c: w for c, w in weights.items()
                if pd_notna(mom[c]) and round(float(mom[c]) * 100, 2) > 0}

    def pd_notna(value):
        return value == value

    outputs = {}
    with tempfile.TemporaryDirectory() as tmp:
        store.split_log = Path(tmp) / "splits.log"
        for label, add_live_gate in (("readme_backtest", False), ("same_backtest_plus_live_momentum_gate", True)):
            gate = patch.object(EtfRotationStrategy, "generate_weights", apply_live_eligibility)
            with patch("pipeline.DataStore", return_value=store), \
                 gate if add_live_gate else contextlib.nullcontext(), \
                 contextlib.redirect_stdout(io.StringIO()) as captured:
                result = run_backtest(codes, strategy="etf_rotation", topk=5, rebalance=5,
                                      start="20190101", end="20260911")
            outputs[label] = {"metrics": result["metrics"], "meta": result["meta"]}
            (OUT / (label + ".log")).write_text(captured.getvalue(), encoding="utf-8")
    return outputs


def main():
    if sys.argv[1:] == ["--self-test"]:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(globals(), {"ROOT": Path(tmp)}):
            file = Path(tmp) / "example.py"
            file.write_text("x = 1\n", encoding="utf-8")
            before = fingerprints()
            assert before == fingerprints()
            file.write_text("x = 2\n", encoding="utf-8")
            assert before != fingerprints(), "content mutation was not detected"
            file.write_text("def broken(\n", encoding="utf-8")
            rejected = False
            try:
                compile(file.read_bytes(), str(file), "exec")
            except SyntaxError:
                rejected = True
            assert rejected, "invalid Python was not rejected"
        print("fingerprint/syntax controls: 3/3")
        return
    if sys.argv[1:]:
        raise SystemExit("Only --self-test is accepted")
    before = fingerprints()
    results = {"selftests": selftests(), "paired_backtests": paired_backtests()}
    after = fingerprints()
    different = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    results["production_source_integrity"] = {"different": different,
                                               "total": len(before), "sha256": after}
    assert not different, different
    syntax_files = sorted(ROOT.rglob("*.py"))
    for p in syntax_files:
        compile(p.read_bytes(), str(p), "exec")
    results["syntax"] = {"errors": 0, "total": len(syntax_files),
                         "paths": [str(p.relative_to(ROOT)) for p in syntax_files]}
    (OUT / "checks.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"selftests": results["selftests"], "backtests": results["paired_backtests"],
                      "source_mismatches": len(different), "source_total": len(before),
                      "syntax_total": len(syntax_files)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
