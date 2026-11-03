"""桌面端与多周期研究策略的无界面回归测试（stdlib unittest）。"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop.service import (
    PROJECT_ROOT,
    BacktestConfig,
    discover_projects,
    ensure_dashboard_server,
    load_settings,
    save_settings,
)
from strategies.builtin import EtfRotationStrategy, MultiHorizonEtfRotationStrategy
from strategies.registry import create_strategy


class _Panel:
    codes: ClassVar[list[str]] = ["A", "B", "C"]

    def get(self, _name):
        return None


def _factors():
    idx = pd.Index(["20260105"])
    return {
        "momentum20": pd.DataFrame([[3.0, 2.0, 1.0]], index=idx, columns=_Panel.codes),
        "momentum60": pd.DataFrame([[2.0, 3.0, 1.0]], index=idx, columns=_Panel.codes),
        "momentum120": pd.DataFrame([[2.0, 3.0, 1.0]], index=idx, columns=_Panel.codes),
    }


class DesktopTests(unittest.TestCase):
    def test_config_roundtrip_and_validation(self):
        cfg = BacktestConfig.from_dict({"kind": "stock", "start": "20230101",
                                       "end": "20260911", "topk": 20})
        self.assertEqual(cfg.kind, "stock")
        self.assertEqual(cfg.topk, 20)
        with self.assertRaises(ValueError):
            BacktestConfig(start="2026-01-01").validate()
        with self.assertRaises(ValueError):
            BacktestConfig(start="20260102", end="20260101").validate()
        with self.assertRaises(ValueError):
            BacktestConfig(topk=0).validate()

    def test_settings_atomic_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "settings.json"
            save_settings({"backtest": {"kind": "etf", "topk": 5}}, path)
            self.assertEqual(load_settings(path)["backtest"]["topk"], 5)
            self.assertFalse(path.with_suffix(".tmp").exists())

    def test_project_discovery(self):
        projects = discover_projects()
        self.assertEqual(projects[0].kind, "etf")
        self.assertTrue((Path(projects[0].root) / "pipeline.py").is_file())
        for project in projects:
            self.assertTrue(Path(project.root).is_dir())

    def test_multi_horizon_is_registered_and_binding(self):
        strategy = create_strategy("etf_rotation_multi", topk=1, rebalance_every=1,
                                   trend_gate=False, trend_window=None, risk_parity=False)
        self.assertIsInstance(strategy, MultiHorizonEtfRotationStrategy)
        weights = strategy.generate_weights("20260105", _factors(), _Panel())
        self.assertEqual(list(weights), ["B"])
        legacy = EtfRotationStrategy(topk=1, rebalance_every=1, trend_gate=False,
                                     trend_window=None, risk_parity=False)
        self.assertEqual(list(legacy.generate_weights("20260105", _factors(), _Panel())), ["A"])
        self.assertEqual(legacy.momentum_windows, (20,))
        with self.assertRaises(ValueError):
            EtfRotationStrategy(momentum_windows=(20, 20))

    def test_research_strategy_cannot_enter_trade_cli(self):
        proc = subprocess.run(
            [sys.executable, "main.py", "simulate", "--strategy", "etf_rotation_multi"],
            check=False, cwd=PROJECT_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("invalid choice", proc.stderr)

    def test_stock_engine_adapter_synthetic(self):
        project = next((p for p in discover_projects() if p.kind == "stock"), None)
        if project is None:
            self.skipTest("未检测到同级 quant2；桌面端可仅运行 ETF 项目")
        proc = subprocess.run(
            [sys.executable, "tools/desktop_stock_smoke.py", "--project-root", project.root],
            check=False, cwd=PROJECT_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)["result"]
        self.assertEqual(result["engine"], "quant2.tools.backtest_stock.run")
        self.assertGreater(result["meta"]["trades"], 0)
        self.assertGreater(len(result["series"]["dates"]), 20)
        self.assertIn("年化收益", result["metrics"])

    def test_dashboard_existing_server_not_restarted(self):
        with (mock.patch("desktop.service.dashboard_server_healthy", return_value=True),
              mock.patch("desktop.service.subprocess.Popen") as popen):
            info = ensure_dashboard_server(PROJECT_ROOT)
        self.assertEqual(info["state"], "existing")
        popen.assert_not_called()

    def test_dashboard_server_start_waits_until_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "server.py").write_text("# test fixture\n", encoding="utf-8")
            process = mock.Mock(pid=12345)
            process.poll.return_value = None
            with (mock.patch("desktop.service.dashboard_server_healthy",
                             side_effect=[False, False, True]),
                  mock.patch("desktop.service.subprocess.Popen", return_value=process) as popen,
                  mock.patch("desktop.service.time.sleep", return_value=None)):
                info = ensure_dashboard_server(root, wait_sec=1.0)
            self.assertEqual(info["state"], "started")
            self.assertEqual(info["pid"], 12345)
            popen.assert_called_once()

    def test_worker_error_is_explicit_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "result.json"
            proc = subprocess.run(
                [sys.executable, "-m", "desktop.worker", "--project-root", str(PROJECT_ROOT),
                 "--config", json.dumps({"start": "bad"}), "--output", str(output)],
                check=False, cwd=PROJECT_ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=20)
            self.assertNotEqual(proc.returncode, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(payload["ok"])
            self.assertIn("YYYYMMDD", payload["error"])


def run_selftest() -> int:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DesktopTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(run_selftest())
