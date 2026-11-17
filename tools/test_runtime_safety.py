"""Offline runtime regressions: synthetic batch children and no broker/UI calls."""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools import daily_job as job

SH = timezone(timedelta(hours=8), "Asia/Shanghai")


class RuntimeSafetyTests(unittest.TestCase):
    def guard(self):
        self.assertIsNotNone(importlib.util.find_spec("tools.trading_session"),
                             "real trading requires a shared session guard")
        return importlib.import_module("tools.trading_session")

    def test_session_guard_rejects_off_hours_holidays_and_missing_calendar(self):
        guard = self.guard()
        with mock.patch("dataprovider.calendar.is_trading_day", return_value=True):
            for hour, minute in [(9, 29), (11, 30), (12, 30), (14, 56), (19, 0)]:
                with self.subTest(hour=hour, minute=minute):
                    with self.assertRaises(RuntimeError):
                        guard.require_trading_session(datetime(2026, 11, 17, hour, minute))
            for hour, minute in [(9, 30), (11, 29), (13, 0), (14, 55)]:
                got = guard.require_trading_session(datetime(2026, 11, 17, hour, minute))
                self.assertEqual(got.utcoffset(), timedelta(hours=8))
            # 01:30 UTC is 09:30 Shanghai, regardless of host timezone.
            got = guard.require_trading_session(datetime(2026, 11, 17, 1, 30, tzinfo=timezone.utc))
            self.assertEqual(got.hour, 9)
        with mock.patch("dataprovider.calendar.is_trading_day", return_value=False):
            with self.assertRaises(RuntimeError):
                guard.require_trading_session(datetime(2026, 11, 17, 10))
        with mock.patch("dataprovider.calendar.is_trading_day", side_effect=ValueError("broken calendar")):
            with self.assertRaises(RuntimeError):
                guard.require_trading_session(datetime(2026, 11, 17, 10))

    def test_session_deadline_is_current_session_end(self):
        guard = self.guard()
        with mock.patch("dataprovider.calendar.is_trading_day", return_value=True):
            self.assertEqual(guard.current_trading_session_deadline(datetime(2026, 11, 17, 10)),
                             datetime(2026, 11, 17, 11, 30, tzinfo=SH))
            self.assertEqual(guard.current_trading_session_deadline(datetime(2026, 11, 17, 14)),
                             datetime(2026, 11, 17, 14, 56, tzinfo=SH))

    def test_local_calendar_never_downloads_when_missing_or_expired(self):
        from dataprovider import calendar
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "trade_calendar.csv"
            with mock.patch.object(calendar, "_CACHE", cache), \
                 mock.patch.object(calendar, "_download", side_effect=AssertionError("network forbidden")):
                with self.assertRaises(RuntimeError):
                    calendar.is_trading_day("20261117", local_only=True)
                cache.write_text("trade_date\n20251117\n20251231\n", encoding="utf8")
                with self.assertRaises(RuntimeError):
                    calendar.is_trading_day("20261117", local_only=True)
                cache.write_text("trade_date\n20261117\n20261231\n", encoding="utf8")
                self.assertTrue(calendar.is_trading_day("20261117", local_only=True))
                self.assertFalse(calendar.is_trading_day("20261118", local_only=True))

    def fake_job(self, folder, at, argv):
        stack = ExitStack()
        self.addCleanup(stack.close)
        for name, leaf in [("STATE", ""), ("LOG", "daily_run.log"),
                           ("STATUS", "last_run.json"), ("ALERT", "ALERT.txt"),
                           ("STATUS_MANUAL", "last_run_manual.json"),
                           ("ALERT_MANUAL", "ALERT_manual.txt")]:
            stack.enter_context(mock.patch.object(job, name, folder / leaf))
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                current = at[0]
                return current.astimezone(tz) if tz else current.replace(tzinfo=None)
        stack.enter_context(mock.patch.object(job, "datetime", Clock))
        stack.enter_context(mock.patch.object(job, "_now", side_effect=lambda: at[0], create=True))
        stack.enter_context(mock.patch("sys.argv", ["daily_job.py"] + argv))
        stack.enter_context(mock.patch("dataprovider.calendar.is_trading_day", return_value=True))
        fake_ui = types.SimpleNamespace(ensure_trade_window=lambda **kw:
            {"ok": True, "state": "READY", "detail": "fixture", "actions": []}, ADVICE={})
        stack.enter_context(mock.patch.dict(sys.modules, {"ths_trade_up": fake_ui}))
        stack.enter_context(mock.patch.object(job, "_preflight", return_value="", create=True))
        stack.enter_context(mock.patch.object(job, "_prepare_data", return_value=(0, ""), create=True))
        stack.enter_context(mock.patch("tools.mail_alert.send", return_value=False))
        stack.enter_context(mock.patch.object(job.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)))
        return stack

    def test_simulated_failure_has_no_production_status_or_preflight_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "isolated"
            stack = self.fake_job(folder, [datetime(2026, 11, 17, 14, 50, tzinfo=SH)], ["--simulate-failure"])
            def forbidden(*args, **kwargs):
                raise AssertionError("simulation entered a real dependency")
            stack.enter_context(mock.patch.object(job, "_preflight", side_effect=forbidden, create=True))
            stack.enter_context(mock.patch.object(job, "_prepare_data", side_effect=forbidden, create=True))
            stack.enter_context(mock.patch("tools.mail_alert.send", side_effect=forbidden))
            self.assertEqual(job.main(), 1)
            self.assertFalse(folder.exists(), "simulation must not create production status paths")

    def test_off_hours_first_attempt_never_reaches_trade_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.fake_job(folder, [datetime(2026, 11, 17, 19, 0, tzinfo=SH)], ["--manual"])
            called = []
            with mock.patch.object(job, "_run_once", side_effect=lambda *a: (called.append(a) or (0, [], ""))):
                self.assertNotEqual(job.main(), 0)
            self.assertEqual(called, [], "off-hours first attempts must fail before connecting")

    def test_retry_wait_is_clipped_and_no_attempt_starts_at_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            at = [datetime(2026, 11, 17, 14, 55, 50, tzinfo=SH)]
            self.fake_job(Path(tmp), at, ["--manual", "--data-ready"])
            called, waits = [], []
            def attempt(*args):
                called.append(at[0])
                return 1, ["同花顺连接失败"], ""
            def sleep(seconds):
                waits.append(seconds)
                at[0] += timedelta(seconds=seconds)
            with mock.patch.object(job, "_run_once", side_effect=attempt), mock.patch.object(job.time, "sleep", side_effect=sleep):
                self.assertNotEqual(job.main(), 0)
            self.assertEqual(called, [datetime(2026, 11, 17, 14, 55, 50, tzinfo=SH)])
            self.assertEqual(waits, [10.0])
            status = json.loads((Path(tmp) / "last_run_manual.json").read_text(encoding="utf8"))
            self.assertEqual(status["attempts"], 1)

    def test_retry_does_not_cross_lunch_or_resume_in_next_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            at = [datetime(2026, 11, 17, 11, 29, 50, tzinfo=SH)]
            self.fake_job(Path(tmp), at, ["--manual", "--data-ready"])
            called, waits = [], []
            def attempt(*args):
                called.append(at[0])
                return 1, ["同花顺连接失败"], ""
            def sleep(seconds):
                waits.append(seconds)
                at[0] += timedelta(seconds=seconds)
            with mock.patch.object(job, "_run_once", side_effect=attempt), mock.patch.object(job.time, "sleep", side_effect=sleep):
                self.assertNotEqual(job.main(), 0)
            self.assertEqual(len(called), 1)
            self.assertEqual(waits, [10.0])

    def test_manual_prepare_uses_64bit_refresh_and_data_ready_still_checks_health(self):
        self.assertTrue(hasattr(job, "_prepare_data"), "manual jobs need a 64-bit data precondition")
        commands, health = [], []
        def run(argv, **kw):
            commands.append(argv)
            return types.SimpleNamespace(returncode=0)
        with mock.patch.dict(os.environ, {"PYTHON64": r"E:\Python\python.exe"}), \
             mock.patch.object(job.subprocess, "run", side_effect=run), \
             mock.patch.object(job, "_check_cached_health", side_effect=lambda: health.append(True) or []):
            self.assertEqual(job._prepare_data(False)[0], 0)
            self.assertEqual(job._prepare_data(True)[0], 0)
        self.assertEqual(len(commands), 1, "batch-provided refresh must not run twice")
        self.assertEqual(commands[0][0], r"E:\Python\python.exe")
        self.assertEqual(len(health), 2, "--data-ready must never bypass cached health checks")

    def test_bad_cached_health_blocks_job_even_with_data_ready(self):
        self.assertTrue(hasattr(job, "_prepare_data"))
        with mock.patch.object(job, "_check_cached_health", return_value=["bad prices"]):
            self.assertEqual(job._prepare_data(True)[0], 3)

    def test_preflight_failure_and_exception_block_child(self):
        for result in ["not ready", RuntimeError("UI unavailable")]:
            with self.subTest(result=result), tempfile.TemporaryDirectory() as tmp:
                stack = self.fake_job(Path(tmp), [datetime(2026, 11, 17, 14, 50, tzinfo=SH)], ["--manual"])
                if isinstance(result, Exception):
                    stack.enter_context(mock.patch.object(job, "_preflight", side_effect=result, create=True))
                else:
                    stack.enter_context(mock.patch.object(job, "_preflight", return_value=result, create=True))
                calls = []
                with mock.patch.object(job, "_run_once", side_effect=lambda *a: (calls.append(a) or (0, [], ""))):
                    self.assertNotEqual(job.main(), 0)
                self.assertEqual(calls, [])

    def test_missing_client_uses_trade_only_startup_then_rechecks_readiness(self):
        snapshots = [
            {"ok": False, "state": "MISSING_PROC", "detail": "missing", "actions": []},
            {"ok": True, "state": "READY", "detail": "ready", "actions": []},
        ]
        ui = types.SimpleNamespace(ensure_trade_window=lambda **kw: snapshots.pop(0), ADVICE={})
        calls = []
        def start(argv, **kwargs):
            calls.append(argv)
            return types.SimpleNamespace(returncode=0, stderr="")
        with mock.patch.dict(sys.modules, {"ths_trade_up": ui}), \
             mock.patch.object(job.subprocess, "run", side_effect=start):
            self.assertEqual(job._preflight(), "")
        self.assertEqual(snapshots, [])
        self.assertEqual(len(calls), 1)
        self.assertIn("-TradeOnly", calls[0])
        self.assertIn(str(ROOT / "tools" / "ths_autostart.ps1"), calls[0])

    def batch_fixture(self, refresh_rc, child_rc):
        folder = Path(tempfile.mkdtemp(prefix="quant-runtime-"))
        self.addCleanup(shutil.rmtree, folder)
        (folder / "tools").mkdir()
        (folder / "state").mkdir()
        (folder / "daily_run.bat").write_bytes((ROOT / "daily_run.bat").read_bytes())
        for name, rc in [("refresh_data", refresh_rc), ("probe_env", 0), ("daily_job", child_rc)]:
            script = "from pathlib import Path\nPath('" + name + ".called').write_text('called')\nraise SystemExit(" + str(rc) + ")\n"
            (folder / "tools" / (name + ".py")).write_text(script.replace("\n", "\n"), encoding="ascii")
        return folder

    def run_batch(self, folder):
        env = dict(os.environ, PYTHON64=sys.executable, PYTHON32=sys.executable)
        return subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(folder / "daily_run.bat")],
                              cwd=folder, env=env, capture_output=True, timeout=30)

    @unittest.skipUnless(os.name == "nt", "Windows batch behavior")
    def test_batch_propagates_real_child_exit_code(self):
        folder = self.batch_fixture(0, 7)
        proc = self.run_batch(folder)
        self.assertTrue((folder / "daily_job.called").exists())
        self.assertEqual(proc.returncode, 7)
        self.assertIn(b"[done] exit=7", (folder / "state/daily_run.log").read_bytes())

    @unittest.skipUnless(os.name == "nt", "Windows batch behavior")
    def test_batch_health_failure_prevents_trade_entry(self):
        folder = self.batch_fixture(3, 7)
        proc = self.run_batch(folder)
        self.assertEqual(proc.returncode, 3)
        self.assertFalse((folder / "daily_job.called").exists())

    @unittest.skipUnless(os.name == "nt", "Windows hidden runner behavior")
    def test_hidden_runner_propagates_synthetic_exit_code(self):
        wrapper = ROOT / "tools/run_scheduled.ps1"
        self.assertTrue(wrapper.is_file(), "scheduled launcher needs exit-preserving hidden wrapper")
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(wrapper), "-CommandExecutable", sys.executable,
                "-CommandArguments", '-c "import sys; sys.exit(7)"', "-WorkingDirectory", tmp],
                capture_output=True, timeout=30)
            self.assertEqual(proc.returncode, 7, proc.stderr.decode("utf8", "replace"))

    @unittest.skipUnless(os.name == "nt", "Windows scheduled action behavior")
    def test_task_installer_preserves_registration_result(self):
        source = (ROOT / "install_schedule.bat").read_bytes()
        self.assertIn(b'net session >nul 2>&1', source)
        self.assertIn(b'powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"', source)
        source = source.replace(b'net session >nul 2>&1', b'cmd /d /c exit 0')
        source = source.replace(b'powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"',
                                b'call fixture_register.bat')
        with tempfile.TemporaryDirectory(prefix="quant installer ") as tmp:
            folder = Path(tmp)
            (folder / "tools/schedule").mkdir(parents=True)
            (folder / "tools/schedule/register_task.ps1").write_text("# fixture", encoding="ascii")
            installer = folder / "install_schedule.bat"
            installer.write_bytes(source)
            for code in (0, 7):
                (folder / "fixture_register.bat").write_bytes(
                    ("@echo off\r\nexit /b %s\r\n" % code).encode("ascii"))
                proc = subprocess.run(["cmd.exe", "/d", "/c", str(installer), "--no-pause"],
                                      cwd=tmp, stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
                self.assertEqual(proc.returncode, code, proc.stdout.decode("utf8", "replace"))

    @unittest.skipUnless(os.name == "nt", "Windows scheduled action behavior")
    def test_task_action_validator_accepts_wrapper_and_rejects_stale_actions(self):
        helper = ROOT / "tools/schedule/task_action.ps1"
        self.assertTrue(helper.is_file(), "registration and health check need one action definition")
        script = """param($Helper, $Repo)
. $Helper
$good = Get-QuantTaskAction -Root $Repo
$answers = @(@(Test-QuantTaskAction -Root $Repo -Action $good).Count)
foreach ($key in @('Execute', 'Arguments', 'WorkingDirectory')) {
    $bad = @{} + $good
    $bad[$key] = 'stale'
    $answers += @(Test-QuantTaskAction -Root $Repo -Action $bad).Count
}
$answers | ConvertTo-Json -Compress
"""
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "check.ps1"
            fixture.write_text(script, encoding="utf-8-sig")
            proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-File",
                                   str(fixture), str(helper), str(ROOT)], capture_output=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf8", "replace"))
            self.assertEqual(json.loads(proc.stdout), [0, 1, 1, 1])

    @unittest.skipUnless(os.name == "nt", "Windows scheduled action behavior")
    def test_declared_scheduled_action_propagates_synthetic_failure(self):
        proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-File",
            str(ROOT / "tools/schedule/register_task.ps1"), "-Describe"],
            capture_output=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf8", "replace"))
        action = json.loads(proc.stdout.decode("utf-8-sig"))
        # Execute exactly the declared launch chain with a harmless cmd child.
        args = action["Arguments"] + ' -CommandExecutable cmd.exe -CommandArguments "/d /c exit 7"'
        launched = subprocess.run('"' + action["Execute"] + '" ' + args,
                                 cwd=action["WorkingDirectory"], capture_output=True, timeout=30)
        self.assertEqual(launched.returncode, 7, launched.stderr.decode("utf8", "replace"))

    @unittest.skipUnless(os.name == "nt", "Windows hidden batch runner behavior")
    def test_hidden_runner_default_batch_waits_in_path_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix="quant runtime ") as tmp:
            marker = Path(tmp) / "done.txt"
            (Path(tmp) / "daily_run.bat").write_bytes(
                b'@echo off\r\n>done.txt echo done\r\nexit /b 7\r\n')
            proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-File",
                str(ROOT / "tools/run_scheduled.ps1"), "-WorkingDirectory", tmp],
                capture_output=True, timeout=30)
            self.assertEqual(proc.returncode, 7, proc.stderr.decode("utf8", "replace"))
            self.assertTrue(marker.is_file())

    @unittest.skipUnless(os.name == "nt", "Windows window-restoration script behavior")
    def test_keepvisible_restores_a_window_that_is_both_hidden_and_minimized(self):
        # Replace only the user32 boundary; execute the real callback and script.
        source = (ROOT / "tools/ths_keepvisible.ps1").read_text(encoding="utf-8-sig")
        fake_api = """Add-Type @'
using System;
using System.Text;
public class ThsRestore {
    public static bool Visible = false;
    public static bool Iconic = true;
    public delegate bool EnumWindowsProc(IntPtr h, IntPtr l);
    public static bool EnumWindows(EnumWindowsProc cb, IntPtr l) { return cb(new IntPtr(1), l); }
    public static int GetWindowTextW(IntPtr h, StringBuilder b, int n) {
        b.Append("网上股票交易系统5.0"); return b.Length;
    }
    public static bool IsIconic(IntPtr h) { return Iconic; }
    public static bool IsWindowVisible(IntPtr h) { return Visible; }
    public static bool ShowWindow(IntPtr h, int n) {
        Visible = true; if (n == 9) { Iconic = false; } return true;
    }
}
'@"""
        source, count = re.subn(r"Add-Type @'.*?\n'@", lambda _: fake_api, source, count=1, flags=re.S)
        self.assertEqual(count, 1)
        source += '\n@{visible=[ThsRestore]::Visible; iconic=[ThsRestore]::Iconic} | ConvertTo-Json -Compress\n'
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "tools").mkdir()
            (folder / "state").mkdir()
            script = folder / "tools/keepvisible-fixture.ps1"
            script.write_text(source, encoding="utf-8-sig")
            proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(script)],
                                  capture_output=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf8", "replace"))
            state = json.loads(proc.stdout)
            self.assertEqual(state, {"visible": True, "iconic": False})


if __name__ == "__main__":
    unittest.main(verbosity=2)
