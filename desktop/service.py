"""桌面端与两个现有回测引擎之间的适配层。

只组合公开接口，不复制策略、成本、风控或绩效公式。每个项目由独立子进程调用，
避免 agentskill/config.py 与 quant2/config.py 在同一解释器中冲突。
"""
from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ProjectInfo:
    key: str
    label: str
    root: str
    kind: str


@dataclass
class BacktestConfig:
    kind: str = "etf"
    strategy: str = "etf_rotation"
    pool: str = "ETF全球"
    start: str = "20190101"
    end: str = "20260911"
    topk: int = 5
    rebalance: int = 5
    weight_mode: str = "tilt"
    tilt_min: float = 0.45
    dd_circuit: bool = True
    vol_target: float = 0.15

    @classmethod
    def from_dict(cls, value: dict) -> BacktestConfig:
        allowed = {f.name for f in fields(cls)}
        cfg = cls(**{k: v for k, v in value.items() if k in allowed})
        return cfg.validate()

    def validate(self) -> BacktestConfig:
        if self.kind not in {"etf", "stock"}:
            raise ValueError("未知项目类型")
        for name in ("start", "end"):
            value = str(getattr(self, name))
            if len(value) != 8 or not value.isdigit():
                raise ValueError("日期必须是 YYYYMMDD")
            datetime.strptime(value, "%Y%m%d")
            setattr(self, name, value)
        if self.start > self.end:
            raise ValueError("开始日期不能晚于结束日期")
        self.topk = int(self.topk)
        self.rebalance = int(self.rebalance)
        if not 1 <= self.topk <= 10000:
            raise ValueError("持仓数量须在 1~10000 之间")
        if not 1 <= self.rebalance <= 250:
            raise ValueError("调仓周期须在 1~250 日之间")
        self.tilt_min = float(self.tilt_min)
        self.vol_target = float(self.vol_target)
        if not 0 <= self.tilt_min <= 1:
            raise ValueError("tilt 下限须在 0~1 之间")
        if not 0 <= self.vol_target <= 1:
            raise ValueError("波动率目标须在 0~1 之间")
        if self.weight_mode not in {"equal", "rank", "tilt"}:
            raise ValueError("未知权重模式")
        return self

    def to_dict(self) -> dict:
        return asdict(self)


def discover_projects(root: Path | str = PROJECT_ROOT) -> list[ProjectInfo]:
    root = Path(root).resolve()
    items = [ProjectInfo("etf", "ETF 轮动 · agentskill", str(root), "etf")]
    quant2 = root.parent.parent / "quant2"
    if (quant2 / "tools" / "backtest_stock.py").is_file():
        items.append(ProjectInfo("stock", "个股多因子 · quant2", str(quant2), "stock"))
    return items


def settings_path() -> Path:
    base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    return base / "QuantWorkbench" / "settings.json"


def load_settings(path: Path | str | None = None) -> dict:
    path = Path(path) if path else settings_path()
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        value = json.load(fh)
    if not isinstance(value, dict):
        raise TypeError("桌面配置格式错误")
    return value


def save_settings(value: dict, path: Path | str | None = None) -> Path:
    path = Path(path) if path else settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def project_status(project: ProjectInfo) -> dict:
    """只读健康摘要；不展示账号、资金、持仓等敏感字段。"""
    root = Path(project.root)
    if project.kind == "etf":
        state = root / "state" / "last_run.json"
        alerts = [root / "state" / "ALERT.txt", root / "state" / "DATA_ALERT.txt"]
    else:
        state = root / "state" / "monitor_latest.json"
        alerts = [root / "state" / "LEARN_ALERT.txt"]
    active_alerts = [p.name for p in alerts if p.exists()]
    out = {"label": project.label, "root": project.root,
           "status": "有告警" if active_alerts else "可研究",
           "alerts": active_alerts, "state_file": None, "updated": None}
    if state.exists():
        out["state_file"] = state.name
        out["updated"] = datetime.fromtimestamp(state.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    return out


def _safe(value):
    """把 numpy/pandas 标量与 NaN 转成严格 JSON 可序列化对象。"""
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if hasattr(value, "item"):
        try:
            return _safe(value.item())
        except (ValueError, TypeError):
            return str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def _series_frame(eq, benchmark=None) -> dict:
    frame = eq.reset_index()
    dates = frame.iloc[:, 0].astype(str).tolist()
    equity = frame["equity"].astype(float).tolist()
    out = {"dates": dates, "equity": equity, "benchmark": None}
    if benchmark is not None:
        out["benchmark"] = benchmark.reindex(eq.index).astype(float).tolist()
    return _safe(out)


def run_etf_backtest(cfg: BacktestConfig, project_root: Path | str = PROJECT_ROOT) -> dict:
    root = str(Path(project_root).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    import config
    from pipeline import run_backtest

    if cfg.pool not in config.RECOMMENDED_POOLS:
        raise ValueError(f"未知 ETF 池：{cfg.pool}")
    out = run_backtest(
        list(config.RECOMMENDED_POOLS[cfg.pool]), strategy=cfg.strategy,
        start=cfg.start, end=cfg.end, topk=cfg.topk, rebalance=cfg.rebalance,
        dd_circuit=cfg.dd_circuit, vol_target=cfg.vol_target,
    )
    result = out["result"]
    return _safe({
        "metrics": out["metrics"], "meta": out["meta"],
        "series": _series_frame(result.equity, result.benchmark),
        "engine": "agentskill.pipeline.run_backtest",
    })


def run_stock_backtest(cfg: BacktestConfig, project_root: Path | str,
                       bars=None) -> dict:
    root = Path(project_root).resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import pandas as pd
    from tools.backtest_stock import DEFAULT_FACTORS, build_features, metrics, run

    if bars is None:
        bars_path = root / "data" / "stockbars" / "bars_total.parquet"
        universe_path = root / "data" / "stockbars" / "universe_all.csv"
        if not bars_path.is_file():
            raise FileNotFoundError(f"缺少个股行情：{bars_path}")
        bars = pd.read_parquet(bars_path)
        if universe_path.is_file():
            universe = pd.read_csv(universe_path, dtype=str)
            st = set(universe.loc[
                universe.is_st.astype(str).str.lower().isin(["true", "1"]), "code"])
            bars = bars[~bars.code.isin(st)]
    bars = bars.copy()
    bars["date"] = bars.date.astype(str).str.replace("-", "", regex=False)
    features = build_features(bars)
    eq, trades, meta = run(
        features, list(DEFAULT_FACTORS), start=cfg.start, end=cfg.end,
        topk=cfg.topk, hold=cfg.rebalance, weight_mode=cfg.weight_mode,
        tilt_min=cfg.tilt_min,
    )
    meta = dict(meta)
    meta.update({"strategy": "stock_multifactor", "start": cfg.start,
                 "end": cfg.end, "trades": len(trades)})
    return _safe({
        "metrics": metrics(eq.equity), "meta": meta,
        "series": _series_frame(eq),
        "engine": "quant2.tools.backtest_stock.run",
    })


def execute_backtest(cfg: BacktestConfig, project_root: Path | str) -> dict:
    cfg.validate()
    if cfg.kind == "etf":
        return run_etf_backtest(cfg, project_root)
    return run_stock_backtest(cfg, project_root)
