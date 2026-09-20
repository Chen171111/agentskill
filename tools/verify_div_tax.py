"""D4 验证：引擎内扣税 vs 换税后价格路径。

核心判据
--------
1. **组合恒等**：`--tax-rate 0.1` 与不扣税的 `trades` 必须**逐位相同**
   （价格路径没变，只是多了一笔现金流出）→ Δ 才干净地等于税负。
2. **税负量级**：与旧口径（`bars_total_tax10.parquet`）的 Δ 应当同向、量级相近。

"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import os
import sys

sys.path.insert(0, r"E:\MyWorkAndProject\量化\agentskill")

import pandas as pd  # noqa: E402

from tools.backtest_stock import build_features, run, metrics  # noqa: E402

ROOT = r"E:\MyWorkAndProject\量化\agentskill"
BARS = os.path.join(ROOT, "data/stockbars/bars_total.parquet")
BARS_TAX = os.path.join(ROOT, "data/stockbars/bars_total_tax10.parquet")
UNI = os.path.join(ROOT, "data/stockbars/universe_all.csv")
DIV = os.path.join(ROOT, "data/stockbars/div_tax_table.parquet")

ALL8 = [("rev20", 1.0), ("rev60", 1.0), ("rev120", 1.0), ("rev5", 1.0),
        ("vol20", 1.0), ("max20", 1.0), ("turn20", 1.0), ("illiq20", 1.0)]
BASE = dict(topk=50, hold=5, weight_mode="equal")
S, E = "20190101", "20260911"


def load(path):
    b = pd.read_parquet(path)
    b["date"] = b.date.astype(str).str.replace("-", "", regex=False)
    u = pd.read_csv(UNI, dtype=str)
    st = set(u.loc[u.is_st.astype(str).str.lower().isin(["true", "1"]), "code"])
    return b[~b.code.isin(st)]


div = pd.read_parquet(DIV)
div["date"] = div.date.astype(str)
print("派现表 {:,} 行 / {} 只 / {}~{}".format(
    len(div), div.code.nunique(), div.date.min(), div.date.max()), flush=True)

print("\n计算因子（bars_total，税前）…", flush=True)
df = build_features(load(BARS))
print("因子完成", flush=True)

res = {}
for tag, kw in [("① 无税", {}), ("② 引擎内扣税 10%", dict(div_tax=div, tax_rate=0.10))]:
    eq, tr, meta = run(df, ALL8, start=S, end=E, **BASE, **kw)
    m = metrics(eq.equity)
    res[tag] = (m, tr, meta)
    print("  {}  年化 {:>6.2f}%  夏普 {:>5.2f}  回撤 {:>7.2f}%  交易 {:>4} 笔  "
          "tax_paid {:.4f}  扣税事件 {:,}".format(
              tag, m["年化收益"], m["夏普比率"], m["最大回撤"], len(tr),
              meta["tax_paid"], meta["n_tax_events"]), flush=True)

a, b = res["① 无税"][1], res["② 引擎内扣税 10%"][1]
same = a.shape == b.shape and (a.values == b.values).all()
print("\n  ── 判据 1：组合恒等 ──")
print("    trades 逐位相同 : {}  （无税 {} 笔 / 扣税 {} 笔）".format(same, len(a), len(b)))
print("    年化差（税负）  : {:+.3f}pp".format(
    res["② 引擎内扣税 10%"][0]["年化收益"] - res["① 无税"][0]["年化收益"]))

print("\n计算因子（bars_total_tax10，旧口径）…", flush=True)
df_tax = build_features(load(BARS_TAX))
eq, tr, meta = run(df_tax, ALL8, start=S, end=E, **BASE)
m = metrics(eq.equity)
print("  {}  年化 {:>6.2f}%  夏普 {:>5.2f}  回撤 {:>7.2f}%  交易 {:>4} 笔".format(
    "③ 旧口径 tax10 路径", m["年化收益"], m["夏普比率"], m["最大回撤"], len(tr)))

c = tr
same_ac = a.shape == c.shape and (a.values == c.values).all()
print("\n  ── 判据 2：旧口径是否真的换了组合 ──")
print("    旧口径 trades 与无税相同 : {}".format(same_ac))
print("    年化：无税 {:.2f}% ｜ 引擎内扣税 {:.2f}% ｜ 旧口径 {:.2f}%".format(
    res["① 无税"][0]["年化收益"], res["② 引擎内扣税 10%"][0]["年化收益"],
    m["年化收益"]))
