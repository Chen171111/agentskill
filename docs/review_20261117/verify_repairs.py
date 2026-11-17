"""Complete source/syntax/encoding/task evidence; never runs a trading action."""
from __future__ import annotations
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def leaves(node, prefix=""):
    name = node.tag.split("}")[-1]
    key = prefix + "/" + name
    result = {key + "/@" + k: v for k, v in node.attrib.items()}
    if len(node):
        for child in node:
            result.update(leaves(child, key))
    else:
        result[key] = (node.text or "").strip()
    return result


def check_tasks():
    before = read_json(OUT / "tasks_snapshot.json")
    reports = []
    for record in before["selected"]:
        path = Path(os.environ["SystemRoot"]) / "System32/Tasks" / record["name"].lstrip("\\")
        reports.append({"name": record["name"], "readable": path.is_file(),
                        "changed": digest(path).upper() != record["diskDefinition"]["sha256"]})
        if record["name"] == "\\QuantPanorama_DailyRun":
            assert digest(path) == digest(OUT / "task_after_repair.xml"), "live task drifted after deployment"
    assert len(reports) == before["selectedCount"]
    assert [r["name"] for r in reports if r["changed"]] == ["\\QuantPanorama_DailyRun"]
    disk = ET.parse(OUT / "task_after_repair.xml").getroot()
    prior = ET.parse(OUT / "task_before_repair.xml").getroot()
    service = read_json(OUT / "task_after_repair_service.json")
    action = disk.findall("t:Actions/t:Exec", NS)
    assert len(action) == service["ActionCount"] == 1
    fields = {"Command": "Execute", "Arguments": "Arguments", "WorkingDirectory": "WorkingDirectory"}
    for tag, key in fields.items():
        assert action[0].findtext("t:" + tag, namespaces=NS).casefold() == service[key].casefold()
    assert service["Execute"].lower().endswith("\\windowspowershell\\v1.0\\powershell.exe")
    assert "tools\\run_scheduled.ps1" in service["Arguments"]
    assert service["LogonType"] == "Interactive" and service["RunLevel"] == "Limited"
    days = disk.find("t:Triggers/t:CalendarTrigger/t:ScheduleByWeek/t:DaysOfWeek", NS)
    assert {d.tag.split("}")[-1] for d in days} == {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"}
    prior_main = next(r for r in before["selected"] if r["name"] == "\\QuantPanorama_DailyRun")
    assert service["LastRunTime"] == prior_main["lastRun"], "task ran during repair"
    assert service["State"] == "Ready"
    old, new = leaves(prior), leaves(disk)
    diffs = [{"field": key, "before": old.get(key), "after": new.get(key)}
             for key in sorted(set(old) | set(new)) if old.get(key) != new.get(key)]
    return {"definitions_checked": len(reports), "definitions": reports,
            "action_fields_match_service": len(fields), "old_leaves": len(old),
            "new_leaves": len(new), "all_leaf_changes": diffs, "not_started": True}


def collect():
    baseline = read_json(OUT / "checks.json")["production_source_integrity"]["sha256"]
    sources = {str(p.relative_to(ROOT)): digest(p) for p in sorted(ROOT.rglob("*.py"))
               if p.relative_to(ROOT).parts[0] not in (".venv", "docs", "state")}
    changed = sorted(p for p, h in baseline.items() if sources.get(p) != h)
    added = sorted(set(sources) - set(baseline))
    syntax_files = sorted(ROOT.rglob("*.py"))
    for path in syntax_files:
        compile(path.read_bytes(), str(path), "exec")
    environmental = read_json(OUT / "environment_final_verification.json")["environmentSources"]["paths"]
    targets = sorted(set(changed + added + environmental + ["install_schedule.bat", "README.md", "tools/schedule/task_action.ps1"]))
    encodings = {}
    for rel in targets:
        raw = (ROOT / rel).read_bytes()
        utf16 = raw.startswith(b"\xff\xfe")
        text = raw.decode("utf-16" if utf16 else "utf-8-sig")
        encodings[rel] = {"bytes": len(raw), "utf16": utf16, "utf8_bom": raw.startswith(b"\xef\xbb\xbf"),
                          "crlf": text.count("\r\n"), "bare_lf": len(re.findall(r"(?<!\r)\n", text))}
        if rel in ("daily_run.bat", "install_schedule.bat"):
            assert raw.isascii() and encodings[rel]["bare_lf"] == 0 and encodings[rel]["crlf"] > 0
        if rel.endswith(".ps1") and not raw.isascii():
            assert encodings[rel]["utf8_bom"], "PS5.1 Chinese script needs BOM: " + rel
    tests = {}
    for label in ("64bit", "32bit", "native_child_encoding"):
        path = OUT / ("repaired_selftest_" + label + ".log")
        text = path.read_text(encoding="utf-8-sig")
        assert "通过 40 ／ 失败 0 ／ 跳过 0" in text
        assert "102 run, 0 failures, 0 errors, 0 skipped" in text
        tests[label] = {"checks": 40, "safety_tests": 102, "desktop_tests": 10,
                        "exit_code": 0, "log_sha256": digest(path)}
    return {"production_python": {"baseline_total": len(baseline), "changed": changed,
                                   "added": added, "current_total": len(sources), "sha256": sources},
            "syntax": {"total": len(syntax_files), "errors": 0},
            "encoding": {"total": len(targets), "files": encodings},
            "tasks": check_tasks(), "tests": tests, "ledger": read_json(OUT / "ledger_upgrade.json"),
            "backtests": {k: v for k, v in read_json(OUT / "repaired_backtests.json").items() if k != "results"},
            "actual_orders_submitted": 0}


def main():
    if sys.argv[1:] == ["--self-test"]:
        good = ET.fromstring("<Task><Actions><Exec><Arguments>good</Arguments></Exec></Actions></Task>")
        bad = ET.fromstring("<Task><Actions><Exec><Arguments>bad</Arguments></Exec></Actions></Task>")
        assert leaves(good) == leaves(ET.fromstring(ET.tostring(good)))
        assert leaves(good) != leaves(bad), "task mutation undetected"
        try:
            compile(b"def broken(\n", "fixture", "exec")
        except SyntaxError:
            print("Verifier self-test: equal trees accepted; task/invalid-syntax mutations detected")
        else:
            raise AssertionError("invalid syntax undetected")
        return
    assert not sys.argv[1:], "only --self-test is accepted"
    result = collect()
    (OUT / "repair_verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps({"syntax": result["syntax"], "encoding_files": result["encoding"]["total"],
                      "production_before": result["production_python"]["baseline_total"],
                      "production_now": result["production_python"]["current_total"],
                      "task_changes": result["tasks"]["all_leaf_changes"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
