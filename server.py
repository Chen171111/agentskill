"""agentskill Web API + Dashboard。

启动：
    python server.py
    # 打开 http://127.0.0.1:8000/
"""
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
import uvicorn

import config
from strategies.registry import list_strategies
from storage.db import TradeDB

app = FastAPI(title="agentskill 自动化量化交易", version="0.1.0")
app.add_middleware(CORSMiddleware,
                   allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
                   allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def auth_middleware(request, call_next):
    """敏感接口鉴权：设置了 ACCESS_TOKEN 时，/api/*（除 health）需 X-Access-Token 匹配。"""
    token = config.ACCESS_TOKEN
    if token and request.url.path.startswith("/api/") and request.url.path != "/api/health":
        if request.headers.get("x-access-token") != token:
            return JSONResponse({"error": "unauthorized"}, status_code=403)
    return await call_next(request)

DASH_PATH = ROOT / "dashboard" / "index.html"
# echarts 等前端依赖本地化（不依赖公网 CDN）：/vendor/echarts.min.js
VENDOR_DIR = ROOT / "dashboard" / "vendor"
if VENDOR_DIR.is_dir():
    from fastapi.staticfiles import StaticFiles
    app.mount("/vendor", StaticFiles(directory=str(VENDOR_DIR)), name="vendor")

ETF_NAMES = config.ETF_NAMES


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


# ============================================================
# 自动交易监控 + 人工操作（2026-10-13 新增）
#   · /api/autotrade   计划/手动运行健康状态、告警、调仓倒计时、日志尾部
#   · /api/rotations   历史调仓明细（按 run 分组的订单 + 当日 live 目标权重）
#   · /api/equity      实盘净值序列
#   · /api/actions/*   后台触发：刷新行情 / 只读试算 / 手动补跑
#   · /api/jobs/*      任务状态与输出
# 安全：沿用全局 auth_middleware（设了 AGENTSKILL_TOKEN 时 /api/* 需令牌）；
#       服务只绑 127.0.0.1。动作全部记入 state/web_actions.log（审计）。
# ============================================================
STATE = ROOT / "state"
LOG_FILE = STATE / "daily_run.log"

# 双解释器约定与 daily_run.bat 一致（env 可覆盖）：
PY64 = os.environ.get("PYTHON64") or r"E:\Python\python.exe"     # 行情刷新（akshare）
PY32 = os.environ.get("PYTHON32") or r"E:\Python32\python.exe"   # 交易链路（同花顺 UIA 必须 32 位）

_JOBS = {}                 # job_id -> dict
_JOBS_LOCK = threading.Lock()
_ACTION_LOCK = threading.Lock()   # 同一时刻只允许一个动作（都碰同花顺/行情缓存）
_ACTION_LOG = STATE / "web_actions.log"


def _tail(path, n=80):
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
        return [l for l in lines if l.strip()][-n:]
    except Exception:
        return []


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _audit(kind, job_id, cmd, rc):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with open(_ACTION_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "kind": kind, "job": job_id, "cmd": cmd, "rc": rc,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _spawn_job(kind, argv, done_lock=None):
    """后台跑一个子进程；done_lock 在进程结束时释放（动作互斥锁跨进程生命周期持有）。"""
    job_id = uuid.uuid4().hex[:8]
    with _JOBS_LOCK:
        _JOBS[job_id] = {"id": job_id, "kind": kind, "cmd": " ".join(argv),
                         "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         "finished": None, "rc": None, "lines": [], "error": ""}

    def _worker():
        with _JOBS_LOCK:
            job = _JOBS.get(job_id)
        if job is None:
            if done_lock:
                done_lock.release()
            return
        rc = None
        try:
            proc = subprocess.Popen(argv, cwd=str(ROOT), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="ignore", bufsize=1)
            for line in proc.stdout:
                with _JOBS_LOCK:
                    job["lines"] = (job["lines"] + [line.rstrip("\n")])[-400:]
            rc = proc.wait()
        except Exception:
            rc = -1
            job["error"] = traceback.format_exc(limit=3)
        job["finished"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        job["rc"] = rc
        _audit(kind, job_id, job["cmd"], rc)
        if done_lock:
            done_lock.release()

    threading.Thread(target=_worker, daemon=True).start()
    return job_id


@app.get("/api/autotrade")
def autotrade():
    """自动交易监控面板数据（前端 30s 轮询）。"""
    now = datetime.now()
    today = now.strftime("%Y%m%d")
    sched = _read_json(STATE / "last_run.json")
    manual = _read_json(STATE / "last_run_manual.json")

    def _alert(name):
        try:
            t = (STATE / name).read_text(encoding="utf-8").strip()
            return t or None
        except Exception:
            return None

    # 调仓倒计时：strategy_since 是「最近一次成功运行后」的计数器；
    # _is_rebalance() 先 +1 再判 % every==0 → 还需 every-(since%every) 次成功运行。
    since, remain, every = None, None, int(config.DEFAULT_REBALANCE)
    try:
        db = TradeDB()
        raw = db.get_state("strategy_since")
        since = int(raw) if raw is not None else None
    except Exception:
        pass
    if since is not None:
        remain = every - (since % every)

    is_td = None
    try:
        from dataprovider.calendar import is_trading_day
        is_td = is_trading_day(now.date())
    except Exception:
        pass
    # daily_job 的「收盘前限窗重试」窗口上限（只重试连接类失败）
    in_window = (now.hour, now.minute) < (14, 56)

    return {
        "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "today": today,
        "is_trading_day": is_td,
        "in_retry_window": in_window,
        "scheduled": sched,
        "manual": manual,
        "alerts": {"auto": _alert("ALERT.txt"), "manual": _alert("ALERT_manual.txt")},
        "mail_last": _read_json(STATE / "alert_mail_sent.json"),
        "strategy_since": since,
        "rebalance_every": every,
        "runs_until_rebalance": remain,
        "log_tail": _tail(LOG_FILE, 80),
    }


@app.get("/api/rotations")
def rotations(limit: int = Query(30)):
    """调仓历史：orders 按 run 前缀（id 形如 20261012145051-xxxx）分组，
    附当日 live 目标权重（shadow_weights，2026-09 起才有）与调仓后总资产。"""
    db = TradeDB()
    with db._session() as c:
        rows = c.execute(
            "SELECT * FROM orders ORDER BY ts ASC, id ASC").fetchall()
        eq = {}
        for r in c.execute("SELECT date, total FROM equity ORDER BY id ASC"):
            eq[r["date"]] = r["total"]
        shadow = {}
        for r in c.execute(
                "SELECT date, weights FROM shadow_weights WHERE variant='live'"):
            shadow[r["date"]] = r["weights"]

    groups = {}
    for r in rows:
        key = r["id"].split("-")[0]
        g = groups.setdefault(key, {"run": key, "date": key[:8],
                                    "ts": r["ts"], "buys": [], "sells": []})
        item = {"code": r["code"], "name": config.ETF_NAMES.get(r["code"], r["code"]),
                "qty": r["filled_qty"] or r["qty"],
                "price": r["filled_price"] or r["price"],
                "status": r["status"]}
        (g["buys"] if r["side"] == "buy" else g["sells"]).append(item)

    out = []
    for g in sorted(groups.values(), key=lambda x: x["ts"], reverse=True)[:limit]:
        d = g["date"]
        g["total_after"] = eq.get(d)
        try:
            g["live_weights"] = json.loads(shadow.get(d) or "{}")
        except Exception:
            g["live_weights"] = {}
        out.append(g)
    return {"rotations": out}


@app.get("/api/equity")
def equity_history():
    """实盘净值序列（每交易日最新一条）。"""
    db = TradeDB()
    with db._session() as c:
        rows = c.execute(
            "SELECT date, cash, market_value, total FROM equity "
            "WHERE id IN (SELECT MAX(id) FROM equity GROUP BY date) "
            "ORDER BY date ASC").fetchall()
    return {"equity": [dict(r) for r in rows]}


def _py32_error():
    if not Path(PY32).exists():
        return JSONResponse(
            {"error": "py32_missing",
             "detail": "32 位 Python 不存在：{}（同花顺 UIA 必须 32 位驱动）".format(PY32)},
            status_code=500)
    return None


def _busy():
    return JSONResponse({"error": "busy", "detail": "已有任务在运行，请等它结束后再试"},
                        status_code=409)


@app.post("/api/actions/refresh")
def action_refresh():
    """64 位 Python 刷新行情 + 数据健康检查（不碰同花顺）。"""
    if not _ACTION_LOCK.acquire(blocking=False):
        return _busy()
    try:
        py = PY64 if Path(PY64).exists() else sys.executable
        job_id = _spawn_job("refresh", [py, "-u", "tools/refresh_data.py"],
                            done_lock=_ACTION_LOCK)
        return {"job_id": job_id, "kind": "refresh"}
    except Exception:
        _ACTION_LOCK.release()
        raise


@app.post("/api/actions/dry")
def action_dry():
    """32 位 Python 只读试算：接同花顺对账+算单，不提交委托、不写账本。
    strategy_since 不会推进 —— 试算零副作用。"""
    err = _py32_error()
    if err:
        return err
    if not _ACTION_LOCK.acquire(blocking=False):
        return _busy()
    try:
        job_id = _spawn_job("dry", [PY32, "-u", "main.py", "simulate", "--dry", "--ths"],
                            done_lock=_ACTION_LOCK)
        return {"job_id": job_id, "kind": "dry"}
    except Exception:
        _ACTION_LOCK.release()
        raise


@app.post("/api/actions/manual")
async def action_manual(request: Request):
    """32 位 Python 手动补跑整条交易链路（--manual：不覆盖 last_run.json、
    不发告警邮件，失败只写 ALERT_manual.txt）。

    ⚠️ 闸门：今日计划任务已成功时，必须显式 confirm_rebalance_skip=true。
    否则一次多余的成功补跑会再次推进 strategy_since → 下一个真正的调仓日被跳过
    （与「收盘后不要手动重跑」同一条纪律，2026-09-18 误报事故后立的）。
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    err = _py32_error()
    if err:
        return err

    sched = _read_json(STATE / "last_run.json")
    today = datetime.now().strftime("%Y%m%d")
    if (sched and sched.get("date") == today and sched.get("ok")
            and not payload.get("confirm_rebalance_skip")):
        return JSONResponse(status_code=409, content={
            "error": "today_scheduled_ok",
            "warning": ("今日计划任务已于 {} 成功。此时再手动补跑一旦成功，会再次推进"
                        "调仓计数 strategy_since，可能导致下一个真正的调仓日被跳过。"
                        "如确认仍要执行，请勾选确认框后再次点击。").format(
                            sched.get("ts", "")),
            "scheduled": sched,
        })
    if not _ACTION_LOCK.acquire(blocking=False):
        return _busy()
    try:
        job_id = _spawn_job("manual",
                            [PY32, "-u", "tools/daily_job.py", "--manual"],
                            done_lock=_ACTION_LOCK)
        return {"job_id": job_id, "kind": "manual"}
    except Exception:
        _ACTION_LOCK.release()
        raise


@app.get("/api/jobs")
def jobs_list():
    with _JOBS_LOCK:
        items = sorted(_JOBS.values(), key=lambda j: j["started"], reverse=True)[:10]
        return {"jobs": [{k: j.get(k) for k in
                          ("id", "kind", "started", "finished", "rc", "error")}
                         for j in items]}


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str, tail: int = Query(200)):
    with _JOBS_LOCK:
        j = _JOBS.get(job_id)
        if not j:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return {"id": j["id"], "kind": j["kind"], "cmd": j["cmd"],
                "started": j["started"], "finished": j["finished"],
                "rc": j["rc"], "error": j["error"],
                "lines": j["lines"][-tail:], "running": j["finished"] is None}


if __name__ == "__main__":
    import webbrowser
    import threading
    # ⚠️ 2026-10-02 验收修复（P1-5）：未设置 ACCESS_TOKEN 时 /api/* 完全开放。
    # 本地 127.0.0.1 使用风险可控，但 share.bat 的公网隧道会把这套接口（含
    # /api/status 的真实持仓）暴露到公网 → 启动时必须醒目提示；
    # share.bat 已改为未设 token 拒绝启动。
    if not config.ACCESS_TOKEN:
        print("=" * 72)
        print("⚠️  警告：未设置 AGENTSKILL_TOKEN，/api/* 接口当前【无鉴权】。")
        print("    本地使用可接受；但【严禁】在此状态下运行 share.bat 公网隧道")
        print("    （会把真实持仓 /api/status 与可触发的 /api/backtest 暴露公网）。")
        print("    设置方法：set AGENTSKILL_TOKEN=<随机长字符串> 后再启动。")
        print("=" * 72)
    threading.Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:8000/")).start()
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")