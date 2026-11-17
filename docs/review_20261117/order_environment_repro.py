"""Offline order/environment defects. All UI and trading subprocesses are mocked.

Only the isolated batch test launches cmd/conhost with synthetic local scripts.
Exit 0 means defects reproduced, not a successful trading acceptance test.
"""
from __future__ import annotations
import contextlib
import copy
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import tools.daily_job as job
import tools.ths_trade_up as up
import trader.ths_uia as ths
import dataprovider.calendar
from trader.broker import Order


def isolated_batch(refresh_rc=0, trade_rc=7):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "tools").mkdir()
        (root / "state").mkdir()
        (root / "daily_run.bat").write_bytes((ROOT / "daily_run.bat").read_bytes())
        (root / "tools/refresh_data.py").write_text("raise SystemExit(%d)\n" % refresh_rc)
        (root / "tools/probe_env.py").write_text("pass\n")
        (root / "tools/daily_job.py").write_text(
            "from pathlib import Path\nPath('trade_invoked.txt').write_text('synthetic')\nraise SystemExit(%d)\n" % trade_rc)
        env = os.environ.copy()
        env.update(PYTHON32=sys.executable, PYTHON64=sys.executable)
        cmd = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
        direct = subprocess.run([cmd, "/d", "/c", "daily_run.bat"], cwd=root, env=env,
                                capture_output=True, timeout=30)
        log = (root / "state/daily_run.log").read_text(encoding="utf-8", errors="replace")
        launched_trade = (root / "trade_invoked.txt").exists()
        windows_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        conhost = windows_root / "System32/conhost.exe"
        if not conhost.exists():
            conhost = windows_root / "Sysnative/conhost.exe"
        wrapped = subprocess.run([str(conhost), "--headless", cmd, "/d", "/c", "daily_run.bat"],
                                 cwd=root, env=env, capture_output=True, timeout=30)
        direct_exit = subprocess.run([cmd, "/d", "/c", "exit 7"], capture_output=True, timeout=30)
        wrapped_exit = subprocess.run([str(conhost), "--headless", cmd, "/d", "/c", "exit 7"],
                                      capture_output=True, timeout=30)
        return {"synthetic_refresh_rc": refresh_rc, "synthetic_trade_rc": trade_rc,
                "direct_batch_rc": direct.returncode, "conhost_batch_rc": wrapped.returncode,
                "direct_explicit_exit_7": direct_exit.returncode,
                "conhost_explicit_exit_7": wrapped_exit.returncode,
                "trade_invoked": launched_trade, "log_has_data_warning": "[WARN]" in log,
                "done_lines": [s.strip() for s in log.splitlines() if "[done]" in s]}


def probe_exit_wrapper():
    result = isolated_batch()
    assert result["direct_batch_rc"] == 0, result
    assert result["direct_explicit_exit_7"] == 7 and result["conhost_explicit_exit_7"] == 0, result
    assert result["conhost_batch_rc"] == 0 and "exit=7" in result["done_lines"][-1], result
    return result


def probe_health_not_blocking():
    result = isolated_batch(refresh_rc=3, trade_rc=0)
    assert result["log_has_data_warning"] and result["trade_invoked"], result
    return result


def confirmation(text, side="buy", qty=100, price=10):
    broker = ths.UiaThsBroker.__new__(ths.UiaThsBroker)
    broker.use_market_price = False
    broker._read_confirm_text = lambda _: text
    clicks = []
    broker._click_dialog_button = lambda dlg, prefixes: clicks.append(prefixes) or True
    order = Order("510300.SH", side, qty, price)
    ok, _ = broker._confirm_dialog(1, expect_code="510300", expect_qty=qty, order=order)
    return {"accepted": ok, "clicked_yes": ths._CONFIRM_YES in clicks,
            "recorded_order_price": order.price}


def probe_confirmation_guards():
    valid = "证券代码：510300(沪深300ETF)\n买入价格：10.000\n买入数量：100"
    assert confirmation(valid)["accepted"]
    assert not confirmation(valid.replace("数量：100", "数量：200"))["accepted"]
    cases = {
        "quantity_missing": "证券代码：510300(沪深300ETF)\n买入价格：10.000",
        "opposite_side": valid.replace("买入", "卖出"),
        "wrong_explicit_limit": valid.replace("10.000", "100.000"),
        "wrong_security_field": valid.replace("证券代码：510300", "证券代码：510500") + "\n上次参考代码：510300",
    }
    results = {name: confirmation(text) for name, text in cases.items()}
    assert all(x["accepted"] and x["clicked_yes"] for x in results.values()), results
    return results


def fill_row(deal, status="已成", price=5, fill=100):
    return {"code": "510300", "side": "买入", "qty": 100, "filled_qty": fill,
            "price": price, "avg_price": price if fill else 0, "deal_id": deal, "status": status}


def probe_ambiguous_fill():
    broker = ths.UiaThsBroker.__new__(ths.UiaThsBroker)
    old = fill_row("synthetic_old_contract")
    new = fill_row("synthetic_new_contract", status="已报", price=10, fill=0)
    first = Order("510300.SH", "buy", 100, 10)
    second = Order("510300.SH", "buy", 100, 10)
    broker.sync_fill(first, rows=[old, new])
    broker.sync_fill(second, rows=[new, old])
    assert first.status == "filled" and first.filled_price == 5
    assert second.status == "submitted" and second.filled_qty == 0
    return {"same_rows_old_first": first.to_dict(), "same_rows_new_first": second.to_dict()}


def probe_cancelled_status():
    broker = ths.UiaThsBroker.__new__(ths.UiaThsBroker)
    order = Order("510300.SH", "buy", 100, 10)
    broker.sync_fill(order, rows=[fill_row("synthetic_cancelled", status="已撤", price=10, fill=0)])
    assert order.status == "submitted", order.to_dict()
    return {"source_status": "已撤", "saved_status": order.status}


@contextlib.contextmanager
def isolated_job(now):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        clock = [now]

        class Clock:
            @classmethod
            def now(cls):
                return clock[0]

        ready = types.SimpleNamespace(ADVICE={}, ensure_trade_window=Mock(return_value={
            "ok": True, "state": "READY", "detail": "synthetic", "actions": []}))
        with contextlib.ExitStack() as stack:
            for key, name in (("STATE", ""), ("LOG", "run.log"), ("STATUS", "status.json"),
                              ("ALERT", "alert.txt"), ("STATUS_MANUAL", "manual.json"),
                              ("ALERT_MANUAL", "manual-alert.txt")):
                stack.enter_context(patch.object(job, key, root / name))
            stack.enter_context(patch.object(job, "datetime", Clock))
            stack.enter_context(patch.object(job.time, "sleep", side_effect=lambda sec: clock.__setitem__(0, clock[0] + timedelta(seconds=sec))))
            stack.enter_context(patch.dict(sys.modules, {"ths_trade_up": ready}))
            stack.enter_context(patch("dataprovider.calendar.is_trading_day", return_value=True))
            stack.enter_context(patch.object(sys, "argv", ["daily_job.py", "--manual"]))
            yield root, clock


def probe_after_close_first_attempt():
    with isolated_job(datetime(2026, 11, 17, 19, 0)) as (_, clock):
        run = Mock(return_value=(0, ["synthetic run completed"], ""))
        with patch.object(job, "_run_once", run):
            rc = job.main()
        assert run.call_count == 1 and rc == 0
        return {"synthetic_start": str(clock[0]), "trading_subprocess_invocations": run.call_count,
                "retry_window_allowed": job._in_retry_window()}


def probe_retry_beyond_window():
    with isolated_job(datetime(2026, 11, 17, 14, 55, 50)) as (_, clock):
        times = []
        def run(*_):
            times.append(clock[0].strftime("%H:%M:%S"))
            return (1, ["同花顺连接失败"], "") if len(times) == 1 else (0, ["synthetic completed"], "")
        with patch.object(job, "_run_once", side_effect=run):
            rc = job.main()
        assert times == ["14:55:50", "14:57:50"] and rc == 0, times
        return {"attempt_times": times, "configured_end": "14:56"}


def probe_false_readiness():
    with patch.object(up.win32gui, "GetWindow", side_effect=lambda h, kind: 2 if h == 1 and kind == up.win32con.GW_CHILD else 0), \
         patch.object(up.win32gui, "GetDlgCtrlID", return_value=1032):
        one_control_passes = up._has_trade_controls(1)
    window = {"hwnd": 1, "title": up.TRADE_TITLE, "pid": 123, "visible": True, "iconic": True, "controls": None}
    with patch.object(up, "find_trade_windows", return_value=[window]), \
         patch.object(up, "trade_proc_pids", return_value=[123]), \
         patch.object(up, "_has_trade_controls", return_value=True):
        snapshot = up.snapshot()
    with patch.object(up, "snapshot", return_value=snapshot), \
         patch.object(up, "show_trade_window") as restore:
        result = up.ensure_trade_window(timeout=0, show=True)
    assert one_control_passes and snapshot["state"] == "READY" and not restore.called
    return {"one_of_four_control_ids_accepted": one_control_passes,
            "minimized_window_state": result["state"], "restore_called": restore.called}


def probe_unverified_receipt():
    broker = ths.UiaThsBroker.__new__(ths.UiaThsBroker)
    broker._switch = lambda _: True
    broker._fill_order_form = lambda *_, **kw: None
    broker._ctrl = lambda *_, **kw: 1
    broker._page_label = lambda: "买入股票"
    broker._bm_click = lambda _: None
    broker._find_confirm_dialog = lambda: (2, [])
    broker._confirm_dialog = lambda *_, **kw: (True, "synthetic confirmation")
    broker._dismiss_info = lambda: False
    order = Order("510300.SH", "buy", 100, 10)
    with patch.object(ths.time, "sleep", return_value=None), \
         patch.object(ths.win32gui, "GetWindowText", return_value="买入"):
        broker._submit_page(order, "buy")
    assert order.status == "submitted"
    return {"receipt_found": False, "saved_status": order.status,
            "broker_contract_field_present": any("contract" in k or "broker_id" in k for k in vars(order))}


PROBES = [probe_exit_wrapper, probe_health_not_blocking, probe_confirmation_guards,
          probe_ambiguous_fill, probe_cancelled_status, probe_after_close_first_attempt,
          probe_retry_beyond_window, probe_false_readiness, probe_unverified_receipt]


def main():
    if sys.argv[1:] == ["--self-test"]:
        valid = "证券代码：510300\n买入价格：10\n买入数量：100"
        assert confirmation(valid)["accepted"]
        assert not confirmation(valid.replace("数量：100", "数量：200"))["accepted"]
        with patch.dict(probe_confirmation_guards.__globals__, {"confirmation": lambda *a, **kw: {"accepted": False}}):
            caught = False
            try:
                probe_confirmation_guards()
            except AssertionError:
                caught = True
            assert caught
        print("confirmation positive/negative/mutation controls: 3/3")
        return
    if sys.argv[1:]:
        raise SystemExit("Only --self-test is accepted")
    result = {}
    for probe in PROBES:
        with contextlib.redirect_stdout(io.StringIO()):
            result[probe.__name__] = probe()
        print(probe.__name__ + ": reproduced")
    output = Path(__file__).with_name("order_environment_results_%dbit.json" % (8 * __import__("struct").calcsize("P")))
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"reproduced": len(result), "total": len(PROBES), "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
