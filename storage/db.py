"""持久化层：SQLite 保存订单、成交记录、每日净值，支持模拟盘状态恢复。"""
import sqlite3
from contextlib import contextmanager
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

    @contextmanager
    def _session(self):
        """打开一个「事务 + 自动关闭连接」的会话，供本模块所有读写使用。

        ⚠️ 为什么不能直接写 `with self._conn() as c:`（2026-09-21 修复）
        -------------------------------------------------------------
        `with sqlite3.connect(...) as c` 作为上下文管理器**只提交/回滚事务，
        并不关闭连接**（连接对象实现了 `__enter__/__exit__` 但 `__exit__` 不 close）。
        于是每次调用都泄漏一个句柄，Windows 下稳定表现为
            PermissionError: [WinError 32] 另一个程序正在使用此文件
        —— 由 `selftest` 的「positions.mark 往返」临时库测试复现；
        常驻的 daily_job 会随运行次数持续累积句柄。
        本方法语义与原写法完全一致（正常提交 / 异常回滚），只是额外关闭连接。
        """
        conn = self._conn()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self):
        with self._session() as c:
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
                    qty INTEGER, cost REAL, peak REAL, mark REAL
                );
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                -- 并行验证（shadow）记录：同一调仓日记录多套目标权重，
                -- 用于「实盘配置 vs 待验证配置」的真实前向对比。
                -- 首个用途：Faber 趋势过滤 开/关（HANDOFF §三 第 2 项）。
                CREATE TABLE IF NOT EXISTS shadow_weights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    variant TEXT NOT NULL,
                    weights TEXT,
                    note TEXT,
                    ts TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_shadow_date
                    ON shadow_weights (date, variant);
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
            # 迁移：positions 增列 mark（池外标的的「最后已知价」）。
            # 为什么需要：mark 让 PortfolioAccount.market_value() 能给**不在策略池**里
            # 的人工持仓定价；旧库缺这一列时按 0 处理（等价于旧行为，不会更差）。
            pcols = [r[1] for r in c.execute("PRAGMA table_info(positions)").fetchall()]
            if pcols and "mark" not in pcols:
                c.execute("ALTER TABLE positions ADD COLUMN mark REAL DEFAULT 0")

    def save_order(self, order):
        with self._session() as c:
            c.execute(
                "INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (order.id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), order.code,
                 order.side, order.qty, order.price, order.reason, order.status,
                 order.filled_price, order.filled_qty, order.fee),
            )

    def save_equity(self, date: str, cash: float, market_value: float, total: float):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._session() as c:
            c.execute(
                "INSERT INTO equity (date, cash, market_value, total, ts) VALUES (?,?,?,?,?)",
                (date, cash, market_value, total, ts),
            )

    def save_positions(self, positions: dict):
        with self._session() as c:
            c.execute("DELETE FROM positions")
            for code, p in positions.items():
                c.execute(
                    "INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?)",
                    (code, p["qty"], p["cost"], p["peak"], p.get("mark", 0.0)),
                )

    def load_positions(self) -> dict:
        with self._session() as c:
            rows = c.execute("SELECT * FROM positions").fetchall()
        return {r["code"]: {"qty": r["qty"], "cost": r["cost"],
                            "peak": r["peak"], "mark": r["mark"] or 0.0}
                for r in rows}

    def load_latest_equity(self) -> dict:
        """读取最近一次记录的现金/市值/总资产，用于跨运行恢复现金。"""
        with self._session() as c:
            row = c.execute(
                "SELECT * FROM equity ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def load_equity_history(self) -> list:
        """读取每个交易日最新一条净值(total)的升序序列，供组合级回撤熔断/波动率目标。"""
        with self._session() as c:
            rows = c.execute(
                "SELECT total FROM equity WHERE id IN "
                "(SELECT MAX(id) FROM equity GROUP BY date) ORDER BY date ASC"
            ).fetchall()
        return [r["total"] for r in rows]

    def get_state(self, key: str):
        """读一个 kv 状态（跨运行持久化，如调仓计数）。"""
        with self._session() as c:
            row = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value):
        with self._session() as c:
            c.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?,?)",
                      (key, str(value)))

    def recent_orders(self, limit=20) -> list:
        with self._session() as c:
            rows = c.execute(
                "SELECT * FROM orders ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 并行验证（shadow） ----------------
    def save_shadow_weights(self, date: str, variant: str, weights: dict,
                            note: str = ""):
        """记录某调仓日、某变体的目标权重。

        ⚠️ 同一 (date, variant) **覆盖**写入 —— 一天内重复运行（如手动补跑）
        不应产生多条记录，否则统计时会重复计数。
        """
        import json
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._session() as c:
            c.execute("DELETE FROM shadow_weights WHERE date=? AND variant=?",
                      (date, variant))
            c.execute(
                "INSERT INTO shadow_weights (date, variant, weights, note, ts) "
                "VALUES (?,?,?,?,?)",
                (date, variant, json.dumps(weights, ensure_ascii=False,
                                           sort_keys=True), note, ts))

    def load_shadow_weights(self, variant: str = None) -> list:
        """返回 [{'date','variant','weights'(dict),'note','ts'}]，按日期升序。"""
        import json
        with self._session() as c:
            if variant:
                rows = c.execute(
                    "SELECT * FROM shadow_weights WHERE variant=? ORDER BY date ASC",
                    (variant,)).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM shadow_weights ORDER BY date ASC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["weights"] = json.loads(d.get("weights") or "{}")
            except Exception:
                d["weights"] = {}
            out.append(d)
        return out