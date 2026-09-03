"""持久化层：SQLite 保存订单、成交记录、每日净值，支持模拟盘状态恢复。"""
import sqlite3
from datetime import datetime

import config


class TradeDB:
    def __init__(self, db_path=None):
        self.db_path = str(db_path or config.DB_PATH)
        self.db_path = str(config.DB_PATH) if not db_path else str(db_path)
        self._init_schema()

    def _conn(self):
        self.db_path_parent = __import__("pathlib").Path(self.db_path).parent
        self.db_path_parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY,
                    ts TEXT, code TEXT, side TEXT, qty INTEGER,
                    price REAL, reason TEXT, status TEXT,
                    filled_price REAL, filled_qty INTEGER, fee REAL
                );
                CREATE TABLE IF NOT EXISTS equity (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    cash REAL, market_value REAL, total REAL,
                    ts TEXT
                );
                CREATE TABLE IF NOT EXISTS positions (
                    code TEXT PRIMARY KEY,
                    qty INTEGER, cost REAL, peak REAL
                );
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            # 迁移：旧版 equity 以 date 为主键、无 id 列会被覆盖，检测到则重建为追加式（保留最后一条现金快照）
            cols = [r[1] for r in c.execute("PRAGMA table_info(equity)").fetchall()]
            if "id" not in cols:
                last = c.execute("SELECT * FROM equity ORDER BY date DESC LIMIT 1").fetchone()
                c.execute("DROP TABLE equity")
                c.execute(
                    """
                    CREATE TABLE equity (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        date TEXT NOT NULL,
                        cash REAL, market_value REAL, total REAL,
                        ts TEXT
                    )
                    """
                )
                if last:
                    c.execute(
                        "INSERT INTO equity (date, cash, market_value, total, ts) VALUES (?,?,?,?,?)",
                        (last["date"], last["cash"], last["market_value"], last["total"],
                         datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    def save_order(self, order):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (order.id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), order.code,
                 order.side, order.qty, order.price, order.reason, order.status,
                 order.filled_price, order.filled_qty, order.fee),
            )

    def save_equity(self, date: str, cash: float, market_value: float, total: float):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._conn() as c:
            c.execute(
                "INSERT INTO equity (date, cash, market_value, total, ts) VALUES (?,?,?,?,?)",
                (date, cash, market_value, total, ts),
            )

    def save_positions(self, positions: dict):
        with self._conn() as c:
            c.execute("DELETE FROM positions")
            for code, p in positions.items():
                c.execute(
                    "INSERT OR REPLACE INTO positions VALUES (?,?,?,?)",
                    (code, p["qty"], p["cost"], p["peak"]),
                )

    def load_positions(self) -> dict:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM positions").fetchall()
        return {r["code"]: {"qty": r["qty"], "cost": r["cost"], "peak": r["peak"]}
                for r in rows}

    def load_latest_equity(self) -> dict:
        """读取最近一次记录的现金/市值/总资产，用于跨运行恢复现金。"""
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM equity ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def load_equity_history(self) -> list:
        """读取每个交易日最新一条净值(total)的升序序列，供组合级回撤熔断/波动率目标。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT total FROM equity WHERE id IN "
                "(SELECT MAX(id) FROM equity GROUP BY date) ORDER BY date ASC"
            ).fetchall()
        return [r["total"] for r in rows]

    def get_state(self, key: str):
        """读一个 kv 状态（跨运行持久化，如调仓计数）。"""
        with self._conn() as c:
            row = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?,?)",
                      (key, str(value)))

    def recent_orders(self, limit=20) -> list:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM orders ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]