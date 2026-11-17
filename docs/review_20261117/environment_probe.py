"""Read-only runtime probe. No broker connections, clicks, login or orders."""
from __future__ import annotations
import contextlib
import csv
import ctypes
import ctypes.wintypes as wt
import hashlib
import importlib
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def dependencies():
    names = {"numpy": "numpy", "pandas": "pandas", "akshare": "akshare",
             "win32gui": "pywin32", "win32con": "pywin32", "win32process": "pywin32",
             "pywinauto": "pywinauto", "PIL": "Pillow", "pytesseract": "pytesseract",
             "fastapi": "fastapi", "uvicorn": "uvicorn", "trader.ths_uia": None}
    result = {}
    for name, distribution in names.items():
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                importlib.import_module(name)
            result[name] = {"ok": True}
            if distribution:
                result[name]["version"] = importlib.metadata.version(distribution)
        except Exception as exc:
            result[name] = {"ok": False, "error": type(exc).__name__ + ": " + str(exc)[:240]}
    return result


def windows():
    import win32gui
    import win32process
    import tools.ths_trade_up as up
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.OpenProcess.restype = wt.HANDLE
    k32.CloseHandle.argtypes = [wt.HANDLE]
    k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    k32.ProcessIdToSessionId.argtypes = [wt.DWORD, ctypes.POINTER(wt.DWORD)]
    k32.IsWow64Process.argtypes = [wt.HANDLE, ctypes.POINTER(wt.BOOL)]
    procs, denied = [], 0
    pids = win32process.EnumProcesses()
    for pid in pids:
        h = k32.OpenProcess(0x1000, False, pid)
        if not h:
            denied += 1
            continue
        try:
            text, length = ctypes.create_unicode_buffer(2048), wt.DWORD(2048)
            if not k32.QueryFullProcessImageNameW(h, 0, text, ctypes.byref(length)):
                continue
            name = Path(text.value).name.lower()
            if name not in ("xiadan.exe", "xiadan-plus.exe", "hexin.exe", "hxmain.exe", "hexinhelper.exe"):
                continue
            sid, wow = wt.DWORD(), wt.BOOL()
            k32.ProcessIdToSessionId(pid, ctypes.byref(sid))
            k32.IsWow64Process(h, ctypes.byref(wow))
            procs.append({"pid": pid, "exe": text.value, "session": sid.value, "wow64": bool(wow.value)})
        finally:
            k32.CloseHandle(h)
    sid = wt.DWORD()
    k32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
    all_windows = []
    win32gui.EnumWindows(lambda h, _: all_windows.append(h) or True, None)
    matching = up.find_trade_windows()
    for w in matching:
        children = []
        win32gui.EnumChildWindows(w["hwnd"], lambda h, _: children.append(h) or True, None)
        controls = {}
        for cid in (1032, 1033, 1034, 1006, 1015, 1016):
            found = [h for h in children if win32gui.GetDlgCtrlID(h) == cid]
            controls[str(cid)] = {"all": len(found), "visible": sum(bool(win32gui.IsWindowVisible(h)) for h in found),
                                  "enabled": sum(bool(win32gui.IsWindowEnabled(h)) for h in found)}
        w["controls_by_id"] = controls
        w["child_count"] = len(children)
        texts = [win32gui.GetWindowText(h) or "" for h in children]
        w["simulation_text_present"] = any("模拟" in t for t in texts)
    # snapshot() has no UI mutation or logging. Do not call ensure/connect here.
    snap = up.snapshot()
    return {"own_session": sid.value, "processes_enumerated": len(pids), "processes_not_queryable": denied,
            "related_processes": procs, "top_windows_enumerated": len(all_windows),
            "trade_windows": matching, "project_readiness_snapshot": snap}


def ocr():
    import pytesseract
    command = pytesseract.pytesseract.tesseract_cmd
    found = shutil.which(command)
    result = {"configured_command": command, "resolved_exe": found}
    for arg in ("--version", "--list-langs"):
        if found:
            proc = subprocess.run([found, arg], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=15)
            result[arg] = {"exit_code": proc.returncode, "output": (proc.stdout + proc.stderr).strip()[:2500]}
    return result


def cached_data():
    import config
    calendar_path = ROOT / "data/trade_calendar.csv"
    with calendar_path.open(encoding="utf-8-sig", newline="") as fh:
        dates = sorted(r["trade_date"] for r in csv.DictReader(fh))
    today = datetime.now().strftime("%Y%m%d")
    target = max(d for d in dates if d < today)
    observations = []
    for code in config.RECOMMENDED_POOLS["ETF全球"]:
        file = ROOT / "data/stocks" / (code + ".csv")
        try:
            with file.open(encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
            seen = set()
            invalid = duplicates = 0
            for row in rows:
                duplicates += row["date"] in seen
                seen.add(row["date"])
                for key in ("open", "high", "low", "close"):
                    try:
                        value = float(row[key])
                        invalid += not math.isfinite(value) or value <= 0
                    except (ValueError, KeyError):
                        invalid += 1
            latest = max(seen) if seen else None
            observations.append({"code": code, "rows": len(rows), "latest": latest,
                                 "fresh": latest is not None and latest >= target,
                                 "duplicate_dates": duplicates, "invalid_price_cells": invalid})
        except Exception as exc:
            observations.append({"code": code, "error": str(exc)})
    return {"today_host_local": today, "calendar_rows": len(dates), "calendar_last": dates[-1],
            "latest_closed_by_project_definition": target, "total_codes": len(observations), "observations": observations}


def state_summary():
    result = {}
    for name in ("last_run.json", "last_run_manual.json"):
        p = ROOT / "state" / name
        if p.exists():
            obj = json.loads(p.read_text(encoding="utf-8-sig"))
            result[name] = {k: obj.get(k) for k in ("ts", "date", "mode", "ok", "exit_code", "attempts", "rebalanced", "skipped")}
    for name in ("ALERT.txt", "ALERT_manual.txt", "DATA_ALERT.txt"):
        p = ROOT / "state" / name
        result[name] = {"exists": p.exists(), "bytes": p.stat().st_size if p.exists() else None}
    p = ROOT / "state/trading.db"
    if p.exists():
        conn = sqlite3.connect(p.as_uri() + "?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only=ON")
            result["ledger"] = {"orders_by_status": dict(conn.execute("SELECT status,COUNT(*) FROM orders GROUP BY status")),
                                "last_order_ts": conn.execute("SELECT MAX(ts) FROM orders").fetchone()[0],
                                "equity_rows": conn.execute("SELECT COUNT(*) FROM equity").fetchone()[0],
                                "latest_equity_date": conn.execute("SELECT MAX(date) FROM equity").fetchone()[0],
                                "position_rows": conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]}
        finally:
            conn.close()
    return result


def xml_fields(text):
    """Preserve all XML leaf values and attributes; compare by semantic path."""
    out = {}
    def visit(node, prefix):
        for key, value in node.attrib.items():
            out[prefix + "/@" + key] = value
        if not len(node):
            out[prefix] = (node.text or "").strip()
        counts = {}
        for child in node:
            counts[child.tag] = counts.get(child.tag, 0) + 1
            visit(child, prefix + "/" + child.tag + "[" + str(counts[child.tag]) + "]")
    root = ET.fromstring(text)
    visit(root, root.tag)
    return out


def compare_xml(left, right):
    a, b = xml_fields(left), xml_fields(right)
    common = sorted(a.keys() & b.keys())
    assert common, "no common fields: cannot claim consistency"
    conflicts, equivalent_times = [], []
    for key in common:
        if a[key] == b[key]:
            continue
        difference = {"path": key, "service": a[key], "disk": b[key]}
        # Explicit equivalence rule: ISO timestamps on these two fields only.
        if key.rsplit("/", 1)[-1].rsplit("}", 1)[-1].split("[", 1)[0] in ("StartBoundary", "EndBoundary"):
            try:
                x, y = (datetime.fromisoformat(v.replace("Z", "+00:00")) for v in (a[key], b[key]))
                if x.tzinfo is not None and y.tzinfo is not None and x == y:
                    equivalent_times.append(difference)
                    continue
            except ValueError:
                conflicts.append(difference)
                continue
        conflicts.append(difference)
    return {"common_fields": len(common), "conflicts": conflicts,
            "equivalent_time_representations": equivalent_times,
            "disk_only": {k: b[k] for k in sorted(b.keys() - a.keys())},
            "service_only": {k: a[k] for k in sorted(a.keys() - b.keys())}}


def task_comparison():
    folder = Path(__file__).parent
    data = json.loads((folder / "tasks_snapshot.json").read_text(encoding="utf-8-sig"))
    result = {"total_tasks_enumerated": data["totalTasksEnumerated"], "tasks": []}
    for task in data["selected"]:
        observed = {"name": task["name"], "sections": {}}
        for key in ("settingsXml", "triggersXml"):
            observed["sections"][key] = compare_xml(task[key], task["diskDefinition"][key])
        observed["actions_match"] = task["diskDefinition"]["actionsMatchExport"]
        result["tasks"].append(observed)
    result["common_fields"] = sum(section["common_fields"] for task in result["tasks"] for section in task["sections"].values())
    result["conflicts"] = sum(len(section["conflicts"]) for task in result["tasks"] for section in task["sections"].values())
    result["disk_only_fields"] = sum(len(section["disk_only"]) for task in result["tasks"] for section in task["sections"].values())
    (folder / "task_definition_comparison.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "tasks"}, ensure_ascii=False))


def main():
    if sys.argv[1:] not in ([], ["--self-test"], ["--tasks-check"]):
        raise SystemExit("Only --self-test and --tasks-check are accepted")
    if sys.argv[1:] == ["--tasks-check"]:
        task_comparison()
        return
    if sys.argv[1:] == ["--self-test"]:
        assert math.isfinite(1.) and not math.isfinite(float("nan"))
        assert not float("nan") > 0
        assert not compare_xml("<s><a>1</a><b>2</b></s>", "<s><b>2</b><a>1</a></s>")["conflicts"]
        assert len(compare_xml("<s><a>1</a></s>", "<s><a>2</a></s>")["conflicts"]) == 1
        assert len(compare_xml("<s><a>1</a></s>", "<s><a>1</a><b>2</b></s>")["disk_only"]) == 1
        time_control = compare_xml("<s><StartBoundary>2026-11-11T14:35:00+08:00</StartBoundary></s>",
                                   "<s><StartBoundary>2026-11-11T06:35:00Z</StartBoundary></s>")
        assert not time_control["conflicts"] and len(time_control["equivalent_time_representations"]) == 1
        print("finite-price and XML order/value/missing-field controls: passed")
        return
    result = {"observed_at_host": datetime.now().astimezone().isoformat(), "executable": sys.executable,
              "python": sys.version, "bits": struct.calcsize("P") * 8, "platform": platform.platform(),
              "environment": {k: os.environ.get(k) for k in ("PYTHON32", "PYTHON64", "PYTHONUTF8", "PYTHONIOENCODING")},
              "dependencies": dependencies()}
    for name, fn in (("windows", windows), ("ocr", ocr), ("cached_data", cached_data), ("state", state_summary)):
        try:
            result[name] = fn()
        except Exception as exc:
            result[name] = {"error": type(exc).__name__ + ": " + str(exc)}
    output = Path(__file__).with_name("environment_" + str(result["bits"]) + "bit.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "bits": result["bits"],
                      "dependencies_ok": sum(x["ok"] for x in result["dependencies"].values()),
                      "dependencies_total": len(result["dependencies"]),
                      "trade_state": result.get("windows", {}).get("project_readiness_snapshot", {}).get("state"),
                      "errors": {k: v["error"] for k, v in result.items() if isinstance(v, dict) and "error" in v}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
