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
    vol_target: float = Query(None),
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


if __name__ == "__main__":
    import webbrowser
    import threading
    threading.Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:8000/")).start()
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")