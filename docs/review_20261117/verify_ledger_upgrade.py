"""Validate every existing table/row before applying additive ledger columns."""
import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import config
from storage.db import TradeDB


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def facts(path, columns=None):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        if columns is None:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            columns = {t: [r[1] for r in conn.execute("PRAGMA table_info(" + quote(t) + ")")]
                       for t in tables}
        signatures = {}
        for table, names in columns.items():
            rows = conn.execute("SELECT " + ",".join(map(quote, names)) + " FROM " + quote(table)).fetchall()
            serial = sorted(repr(tuple(row)) for row in rows)
            signatures[table] = (len(rows), hashlib.sha256("\n".join(serial).encode("utf8")).hexdigest())
        return columns, signatures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        assert not args.apply, "self-test cannot apply a production upgrade"
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / "fixture.db"
            TradeDB(fixture).save_positions({"TEST": {"qty": 100, "cost": 10., "peak": 10.}})
            columns, before = facts(fixture)
            assert facts(fixture, columns)[1] == before
            with closing(sqlite3.connect(fixture)) as conn:
                conn.execute("UPDATE positions SET qty=200 WHERE code='TEST'")
                conn.commit()
            assert facts(fixture, columns)[1] != before, "mutation was not detected"
            print("Ledger verifier self-test: identical rows accepted; changed quantity detected")
        return
    path = Path(config.DB_PATH).resolve()
    output = {"copy_validated": False, "applied": False, "no_trading_calls": True}
    # Taking the same per-ledger lock prevents a trading writer during backup,
    # full-table comparison and the additive schema upgrade.
    with TradeDB(path, read_only=True).run_lock(), tempfile.TemporaryDirectory() as tmp:
        columns, before = facts(path)
        copy_path = Path(tmp) / "copy.db"
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as src, \
             closing(sqlite3.connect(copy_path)) as dst:
            src.backup(dst)
        TradeDB(copy_path)
        assert facts(copy_path, columns)[1] == before, "copy migration changes existing facts"
        output.update(copy_validated=True, tables_checked=len(columns))
        if args.apply:
            backup = ROOT / "state/ledger_backups" / ("pre_audit_repair_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".db")
            backup.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as src, \
                 closing(sqlite3.connect(backup)) as dst:
                src.backup(dst)
            assert facts(backup, columns)[1] == before
            TradeDB(path)
            assert facts(path, columns)[1] == before, "live migration changes existing facts"
            output.update(applied=True, backup=str(backup.relative_to(ROOT)),
                          unchanged_tables=len(columns), changed_existing_rows=0)
        output["new_columns"] = {table: sorted(set(names) - set(columns.get(table, [])))
                                 for table, names in facts(path)[0].items()}
    Path(__file__).with_name("ledger_upgrade.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
