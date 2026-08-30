"""agentskill Web API + Dashboard。

启动：
    python server.py
    # 打开 http://127.0.0.1:8000/
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
import uvicorn

import config
from strategies.registry import list_strategies
from storage.db import TradeDB

app = FastAPI(title="agentskill 自动化量化交易", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])

DASH_PATH = ROOT / "dashboard" / "index.html"

# ETF 名称映射（用于"今日建议"展示）
ETF_NAMES = {
    "510300.SH": "沪深300ETF", "510500.SH": "中证500ETF", "510050.SH": "上证50ETF",
    "159915.SZ": "创业板ETF", "159928.SZ": "消费ETF", "159920.SZ": "恒生ETF",
    "513100.SH": "纳指ETF", "513500.SH": "标普500ETF", "518880.SH": "黄金ETF",
    "510880.SH": "红利ETF", "511010.SH": "国债ETF",
}


def _today_picks():
    """运行 etf_rotation 策略，返回最新交易日的建议持仓（含风险平价权重与动量）。"""
    from pipeline import DEFAULT_FACTORS
    from dataprovider.store import DataStore
    from dataprovider.panel import build_panel
    from factors.engine import compute_factors
    from strategies.registry import create_strategy

    codes = list(config.RECOMMENDED_POOLS["ETF全球"])
    store = DataStore()
    store.ensure(codes)
    panel = build_panel(store, codes)
    factors = compute_factors(panel, DEFAULT_FACTORS)
    strat = create_strategy("etf_rotation", topk=5, rebalance_every=5)
    # 触发一次调仓，拿到"当前"目标权重
    strat._since = strat.rebalance_every - 1

    last = panel.dates[-1]
    weights = strat.generate_weights(last, factors, panel) or {}
    mom_df = factors.get("momentum20")
    picks = []
    for c, wt in sorted(weights.items(), key=lambda x: -x[1]):
        mom = None
        if mom_df is not None and last in mom_df.index and c in mom_df.columns:
            v = mom_df.loc[last, c]
            mom = round(float(v) * 100, 2) if v == v else None
        picks.append({"code": c, "name": ETF_NAMES.get(c, c),
                      "weight": round(wt, 4), "momentum20": mom})
    return picks, last


@app.get("/", response_class=HTMLResponse)
def index():
    if DASH_PATH.exists():
        return FileResponse(str(DASH_PATH))
    return "<h1>agentskill</h1><p>未找到 dashboard/index.html</p>"


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    from fastapi.responses import Response
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
           '<rect width="100" height="100" rx="20" fill="#2b3a67"/>'
           '<text x="50" y="68" font-size="50" text-anchor="middle" fill="#fff">A</text>'
           '</svg>')
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/strategies")
def strategies():
    return {"strategies": list_strategies()}


@app.get("/api/pools")
def pools():
    from dataprovider.store import fetch_index_cons_sample
    items = [{"name": k, "codes": v} for k, v in config.RECOMMENDED_POOLS.items()]
    items.append({"name": "个股动量", "codes": fetch_index_cons_sample(n=20)})
    return {"pools": items}


@app.api_route("/api/backtest", methods=["GET", "POST"])
def backtest(
    codes: str = Query(None),
    pool: str = Query(None),
    strategy: str = Query("momentum"),
    start: str = Query("20190101"),
    end: str = Query("20251231"),
    topk: int = Query(5),
    rebalance: int = Query(5),
    benchmark: str = Query(None),
    timing: str = Query(None),
    dd_circuit: bool = Query(config.DEFAULT_DD_CIRCUIT),
    vol_target: float = Query(config.DEFAULT_VOL_TARGET),
):
    from pipeline import run_backtest
    if pool == "个股动量":
        from dataprovider.store import fetch_index_cons_sample
        code_list = fetch_index_cons_sample(n=20)
    elif pool and pool in config.RECOMMENDED_POOLS:
        code_list = list(config.RECOMMENDED_POOLS[pool])
    else:
        code_list = [c.strip() for c in (codes or "").split(",") if c.strip()]
    if not code_list:
        return {"error": "请提供 codes 或 pool"}

    out = run_backtest(
        code_list, strategy=strategy, start=start, end=end,
        topk=topk, rebalance=rebalance, benchmark=benchmark, timing=timing,
        dd_circuit=dd_circuit, vol_target=vol_target,
    )
    result = out["result"]
    eq = result.equity.reset_index()
    payload = {
        "metrics": out["metrics"],
        "dates": eq["date"].astype(str).tolist(),
        "equity": eq["equity"].round(6).tolist(),
        "value": eq["value"].round(2).tolist(),
        "meta": out["meta"],
    }
    if result.benchmark is not None and len(result.benchmark):
        payload["benchmark"] = result.benchmark.round(6).tolist()
    return payload


@app.get("/api/status")
def status():
    db = TradeDB()
    pos = db.load_positions()
    return {
        "positions": [
            {"code": c, "qty": p["qty"], "cost": round(p["cost"], 4),
             "peak": round(p["peak"], 4)} for c, p in pos.items()
        ],
        "orders": db.recent_orders(20),
    }


@app.get("/api/market")
def market():
    """市场全景：大盘指数快照 + 两市成交额 + ETF轮动今日建议。"""
    resp = {"indexes": [], "turnover_yi": None, "picks": [], "date": None}

    try:
        import akshare as ak
        spot = ak.stock_zh_index_spot_sina()
        # 指数展示：上证/深证/创业板；两市成交额=上证+深证（创业板属深证子集，不计入）
        turnover = 0.0
        turnover_ids = {"sh000001", "sz399001"}
        for code, name in (("sh000001", "上证指数"), ("sz399001", "深证成指"),
                           ("sz399006", "创业板指")):
            row = spot[spot["代码"] == code]
            if row.empty:
                continue
            r = row.iloc[0]
            resp["indexes"].append({
                "code": code, "name": name,
                "price": round(float(r["最新价"]), 2),
                "pct": round(float(r["涨跌幅"]), 2),
            })
            if code in turnover_ids:
                turnover += float(r["成交额"])
        resp["turnover_yi"] = round(turnover / 1e8)  # 元 → 亿
    except Exception as e:
        resp["market_error"] = str(e)[:200]

    try:
        resp["picks"], resp["date"] = _today_picks()
    except Exception as e:
        resp["picks_error"] = str(e)[:200]
    return resp


if __name__ == "__main__":
    import webbrowser
    import threading
    threading.Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:8000/")).start()
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")