# -*- coding: utf-8 -*-
"""`joinquant/jq_dividend.py` 的**离线自检**（不需要聚宽账号）。

为什么要写这个
--------------
本项目被「回测与实现不一致」坑过三次（`hyst` 静默退化、引擎 `topk*2` 静默跳过、
ETF 份额折算未复权）。把策略移植到聚宽属于**最容易出现口径漂移**的动作。

本脚本做两件事：

**一、移植一致性** —— 聚宽路径（`jq_dividend._load_dividends` + `_yield_at`）
    与本地口径（`tools/test_dividend_factor.build_yield_panel`）是否**逐位一致**？
    做法：伪造 `jqdata` 模块，把**本地分红数据按聚宽字段名**喂给聚宽路径，
    两边算同一个 `dps_ttm` / `n_div3`，逐值比较。
    对照网格 = 每只抽样股票自己的全部除权除息日（股息率的所有跳变点都在这里）。

**二、送转调整方向的决定性检验** —— 本地 `dps_ttm` 的送转调整是

        adj[i] = Π_{j>i} (1 + r_j)      # j 遍历全部分红历史，**不设上界**，且是**乘**

    但「送转是价值中性的」→ **在纯送转的除权日前后，股息率不应该跳变**。
    本检验就测这一条：

      · 若 dy 在送转日跳变 ×(1+r)  → 现行口径**方向错了**（应除不该乘，且应止于 t）
      · 若 dy 基本连续             → 现行口径成立

    三种口径对比（`D_i` = 每股税前现金分红，`r_i` = 送股+转增/10）：

      | 口径 | 公式 | 含义 |
      |---|---|---|
      | **L 现行** | `Σ D_i · Π_{k>i, 全部}(1+r_k)` | 本地/聚宽现在用的（`jq_dividend.py` 默认） |
      | W 窗口 | `Σ D_i · Π_{i<k≤t}(1+r_k)` | 只把「未来送转」剔出去，方向仍为乘 |
      | **C 正确** | `Σ D_i / Π_{i≤k≤t}(1+r_k)` | 换算到 **t 时刻**股本口径：价值中性 |

    （C 的推导：在 `i` 除权日持有 1 股、到 `t` 时变成 `Π_{i≤k≤t}(1+r_k)` 股，
      那笔现金摊到「当前每股」上就要**除**这个因子。）

用法
----
    PY="C:/Users/XiaoQi/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"
    cd "E:/MyWorkAndProject/量化/agentskill"
    $PY joinquant/_verify_core.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from types import SimpleNamespace

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

N_CODES = 800          # 一、抽样股票数
N_SPLIT = 400          # 二、送转事件抽样股票数
SEED = 20260917
TRADE_END = '2026-09-11'
RAW = [None]           # 由 main() 注入的分红长表（聚宽字段名）
ADJ_MODE_DEFAULT = 'legacy'   # 与 jq_dividend.py 的默认值保持一致


class _Any:
    """什么都接受、什么都返回自己的哑对象 —— 只为了让 query(...).filter(...) 能构造。"""

    def __getattr__(self, k):
        return _Any()

    def __call__(self, *a, **k):
        return _Any()

    def __eq__(self, o):
        return _Any()

    def __ne__(self, o):
        return _Any()

    def __ge__(self, o):
        return _Any()

    def __le__(self, o):
        return _Any()

    def __lt__(self, o):
        return _Any()

    def __gt__(self, o):
        return _Any()


class _FakeFinance:
    STK_XR_XD = _Any()

    def run_offset_query(self, q):
        return RAW[0]

    def run_query(self, q):
        return RAW[0]


def _load_jq_module():
    """伪造 jqdata / g / log，把 `joinquant/jq_dividend.py` 当普通模块 import 进来。"""
    jq = types.ModuleType('jqdata')
    jq.query = lambda *a, **k: _Any()
    jq.finance = _FakeFinance()
    sys.modules['jqdata'] = jq
    path = os.path.join(ROOT, 'joinquant', 'jq_dividend.py')
    spec = importlib.util.spec_from_file_location('jq_dividend', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.log = SimpleNamespace(info=lambda *a, **k: None,
                              warning=lambda *a, **k: None,
                              error=lambda *a, **k: None)
    mod.g = SimpleNamespace(div=None)
    return mod


def _to_jq_fields(div: pd.DataFrame) -> pd.DataFrame:
    """东财分红字段 → 聚宽 `STK_XR_XD` 字段名（映射见 joinquant/README.md §三）。"""
    return pd.DataFrame({
        'code': div['code'].values,
        'a_xr_date': div['EX_DIVIDEND_DATE'].values,
        'bonus_ratio_rmb': div['PRETAX_BONUS_RMB'].values,      # 每10股派息（税前）
        'dividend_ratio': div['BONUS_IT_RATIO'].values,         # 东财「送转合计」≙ 送股+转增
        'transfer_ratio': 0.0,
        'bonus_cancel_pub_date': pd.NaT,
    })


# ================================================================ 三种口径
def prep_events(div_c: pd.DataFrame) -> dict:
    """把一只股票的分红记录预处理成数组（按除权日升序）。"""
    g = div_c.sort_values('a_xr_date')
    ex = g.a_xr_date.values.astype('datetime64[D]')
    cash, r = g.cash.values, g.r.values
    n = len(g)
    sfx = np.ones(n)                      # Π_{k>i}(全部)
    acc = 1.0
    for i in range(n - 1, -1, -1):
        sfx[i] = acc
        acc *= (1.0 + r[i])
    cum_prev = np.concatenate([[1.0], np.cumprod(1.0 + r)[:-1]])   # Π_{k<i}
    return {'ex': ex, 'cash': cash, 'r': r, 'sfx': sfx, 'cum_prev': cum_prev}


def dps_modes(ev: dict, t: np.datetime64):
    """返回 (legacy, window, correct, n_div3)。"""
    ex, cash = ev['ex'], ev['cash']
    tt = np.datetime64(t, 'D')
    le = ex <= tt
    Pt = float(np.prod(1.0 + ev['r'][le])) if le.any() else 1.0
    win = le & (ex > tt - np.timedelta64(365, 'D'))
    if not win.any():
        return 0.0, 0.0, 0.0, int(((ex > tt - np.timedelta64(1095, 'D')) & le & (cash > 0)).sum())
    d_leg = float(np.sum(cash[win] * ev['sfx'][win]))
    d_win = float(np.sum(cash[win] * (Pt / np.cumprod(1.0 + ev['r'])[win])))
    d_cor = float(np.sum(cash[win] * (ev['cum_prev'][win] / Pt)))
    n3 = int(np.sum(win | ((ex > tt - np.timedelta64(1095, 'D')) & le)) )
    n3 = int(np.sum(le & (ex > tt - np.timedelta64(1095, 'D')) & (cash > 0)))
    return d_leg, d_win, d_cor, n3


def main() -> int:
    print('=' * 96)
    print('  joinquant/jq_dividend.py · 离线自检')
    print('=' * 96, flush=True)

    div = pd.read_parquet(os.path.join(ROOT, 'data/dividends/bonus_all.parquet')).copy()
    div['code'] = div.code.astype(str)
    div['cash'] = pd.to_numeric(div.PRETAX_BONUS_RMB, errors='coerce').fillna(0.0) / 10.0
    div['r'] = pd.to_numeric(div.BONUS_IT_RATIO, errors='coerce').fillna(0.0) / 10.0
    print('  分红原始 {} 条 / {} 只'.format(len(div), div.code.nunique()), flush=True)

    uni = pd.read_csv(os.path.join(ROOT, 'data/stockbars/universe_all.csv'), dtype=str)
    pool = np.array(sorted({c for c in uni.code.dropna().tolist()
                            if c in set(div.code.tolist())}), dtype=object)
    rng = np.random.default_rng(SEED)
    codes = sorted(pool[rng.choice(len(pool), min(N_CODES, len(pool)), replace=False)].tolist())
    print('  抽样 {} 只（seed={}）'.format(len(codes), SEED), flush=True)

    # ============================== 一、移植一致性 ==============================
    sub = div[div.code.isin(codes)].copy()
    sub['d'] = sub.EX_DIVIDEND_DATE.astype(str).str.replace('-', '', regex=False)
    sub = sub[sub.d.str.len() == 8]
    grid = sub[['code', 'd']].drop_duplicates().rename(columns={'d': 'date'})
    grid['date'] = grid.date.astype(str)
    grid = grid[(grid.date <= TRADE_END.replace('-', '')) | (grid.date <= '20261231')]
    # ⚠️ 只保留 ≤ TRADE_END：聚宽路径只预取到 TRADE_END，未来的预案除权日两边本就该不同
    grid = grid[grid.date <= TRADE_END.replace('-', '')]
    grid['close'] = 1.0
    grid = grid.sort_values(['code', 'date']).reset_index(drop=True)

    RAW[0] = _to_jq_fields(div)
    mod = _load_jq_module()
    prep = mod._load_dividends('2019-01-02', TRADE_END)
    mod.g.div = prep

    from tools.test_dividend_factor import build_yield_panel
    loc = build_yield_panel(grid, div, price_col='close')

    print()
    print('=' * 96)
    print('  一、移植一致性：聚宽路径 vs 本地 build_yield_panel')
    print('=' * 96)
    print('  聚宽路径分红预处理 {} 条 ｜ 对照网格 {} 个 (股票,除权日)'.format(
        len(prep), len(grid)), flush=True)
    ok_all, n_cmp, bad = True, 0, []
    for dt in sorted(loc.date.unique()):
        s = loc[loc.date == dt]
        cs = s.code.tolist()
        dps_jq, n3_jq = mod._yield_at(dt, cs)
        a = s.set_index('code')
        sp = a['dps_ttm'].reindex(cs).fillna(0.0)
        sn = a['n_div3'].reindex(cs).fillna(0).astype(int)
        bp = dps_jq.reindex(cs).fillna(0.0)
        bn = n3_jq.reindex(cs).fillna(0).astype(int)
        n_cmp += len(cs)
        dmax = float((sp - bp).abs().max())
        nbadcount = int((sn != bn).sum())
        if dmax >= 1e-9 or nbadcount:
            ok_all = False
            bad.append((dt, len(cs), dmax, nbadcount))
    print('  比对 {} 个组合 ｜ 不一致 {} 个'.format(n_cmp, len(bad)))
    for dt, n, dmax, nb in bad[:10]:
        print('    ❌ {}  股票 {}  dps最大差 {:.3e}  n_div3不符 {}'.format(dt, n, dmax, nb))
    print('  → {}'.format(
        '✅ 逐位一致 —— 移植没引入口径漂移，聚宽这边的股息率算法与本地等价'
        if ok_all else '❌ 存在不一致，移植有 bug'))

    # ============================== 二、送转方向检验 ==============================
    print()
    print('=' * 96)
    print('  二、送转调整方向：纯送转除权日前后，dy 应该连续（送转是价值中性的）')
    print('=' * 96)
    ev = div[((div.r > 0) | (div.cash > 0))
             & div.EX_DIVIDEND_DATE.notna()
             & div.ASSIGN_PROGRESS.astype(str).str.contains('实施', na=False)].copy()
    ev['a_xr_date'] = pd.to_datetime(ev.EX_DIVIDEND_DATE)
    ev_all = {}
    for c, g in ev.groupby('code'):
        ev_all[c] = prep_events(g)

    # 找「纯送转」事件（该日 cash==0 且 r>0）且该日之前 TTM 窗口内有现金分红
    cand = ev[(ev.r > 0) & (ev.cash <= 0)]
    cand = cand[cand.code.isin(set(codes))]
    cand['d'] = ev['a_xr_date']
    cand = cand[cand.d <= pd.Timestamp(TRADE_END)]
    hits = []
    for r_ in cand.sort_values('r', ascending=False).itertuples(index=False):
        ev = ev_all.get(r_.code)
        if ev is None:
            continue
        d = np.datetime64(r_.d, 'D')
        # 该日之前 365 天内要有现金分红（这样 dy 才非零、可比）
        prior = (ev['ex'] < d) & (ev['ex'] > d - np.timedelta64(365, 'D')) & (ev['cash'] > 0)
        if prior.sum() >= 1:
            hits.append((r_.code, d, r_.r))
        if len(hits) >= N_SPLIT:
            break
    print('  找到「纯送转 + 前1年内有现金分红」事件 {} 个'.format(len(hits)), flush=True)
    if not hits:
        print('  （样本不足，跳过）')
        return 0

    sc = sorted({h[0] for h in hits})
    print('  涉及 {} 只股票，读取不复权日线…'.format(len(sc)), flush=True)
    bars = pd.read_parquet(os.path.join(ROOT, 'data/stockbars/bars_bfq.parquet'),
                           columns=['code', 'date', 'close'],
                           filters=[('code', 'in', list(sc))])
    bars['code'] = bars.code.astype(str)
    bars['date'] = bars.date.astype(str).str.replace('-', '', regex=False)
    px = {}
    for c, g in bars.groupby('code'):
        px[c] = g.set_index('date')['close'].sort_index()
    print('  日线 {} 行 / {} 只'.format(len(bars), len(px)), flush=True)

    print()
    print('  {:<12}{:<11}{:>7}{:>11}{:>11}{:>11}{:>11}{:>11}'.format(
        '代码', '送转除权日', '1+r', 'dy_L前', 'dy_L后', '比值L', '比值W', '比值C'))
    print('  ' + '-' * 88)
    rows = []
    for c, d, r_ in hits:
        s = px.get(c)
        if s is None or not len(s):
            continue
        dd = pd.Timestamp(d)
        ds = s.index
        before = ds[ds < dd.strftime('%Y%m%d')]
        after = ds[ds >= dd.strftime('%Y%m%d')]
        if not len(before) or not len(after):
            continue
        d0, d1 = before[-1], after[0]
        ev = ev_all[c]
        # legacy / correct 走**模块真身**（验证发布出去的那份代码，不是测试里的复刻）
        mod.ADJ_MODE = 'legacy'
        dl0 = float(mod._yield_at(d0, [c])[0].get(c, 0.0))
        dl1 = float(mod._yield_at(d1, [c])[0].get(c, 0.0))
        mod.ADJ_MODE = 'correct'
        dc0 = float(mod._yield_at(d0, [c])[0].get(c, 0.0))
        dc1 = float(mod._yield_at(d1, [c])[0].get(c, 0.0))
        mod.ADJ_MODE = ADJ_MODE_DEFAULT
        _, dw0, _, _ = dps_modes(ev, np.datetime64(pd.Timestamp(d0), 'D'))
        _, dw1, _, _ = dps_modes(ev, np.datetime64(pd.Timestamp(d1), 'D'))
        p0, p1 = float(s[d0]), float(s[d1])
        if p0 <= 0 or p1 <= 0 or dc0 <= 0 or dl0 <= 0:
            continue
        yl0, yl1 = dl0 / p0 * 100, dl1 / p1 * 100
        yw0, yw1 = dw0 / p0 * 100, dw1 / p1 * 100
        yc0, yc1 = dc0 / p0 * 100, dc1 / p1 * 100
        rows.append((c, d1, r_,
                     yl0, yl1, yl1 / yl0 if yl0 else np.nan,
                     yw1 / yw0 if yw0 else np.nan,
                     yc1 / yc0 if yc0 else np.nan))
    if not rows:
        print('  （无可用样本）')
        return 0
    w = pd.DataFrame(rows, columns=['code', 'd', 'r', 'yl0', 'yl1',
                                    'ratio_L', 'ratio_W', 'ratio_C'])
    for r_ in w.head(15).itertuples(index=False):
        print('  {:<12}{:<11}{:>7.2f}{:>11.2f}{:>11.2f}{:>11.2f}{:>11.2f}{:>11.2f}'.format(
            r_.code, r_.d, 1 + r_.r, r_.yl0, r_.yl1, r_.ratio_L, r_.ratio_W, r_.ratio_C))
    print('  …')
    print()
    print('  样本 {} 个 ｜ 期望：(1+r) 与各口径比值的对比'.format(len(w)))
    exp = (1.0 + w.r)
    for nm, col in (('L 现行', 'ratio_L'), ('W 窗口', 'ratio_W'), ('C 正确', 'ratio_C')):
        v = w[col].replace([np.inf, -np.inf], np.nan).dropna()
        # 与「期望口径」比：L 应该≈(1+r)（=错）；C 应该≈1（=对）
        dev_from_1 = float((v - 1.0).abs().median())
        dev_from_1r = float((v - exp.reindex(v.index)).abs().median())
        print('  {:<8} 中位比值 {:.3f} ｜ 偏离 1 的中位 {:.3f} ｜ 偏离 (1+r) 的中位 {:.3f}'
              .format(nm, float(v.median()), dev_from_1, dev_from_1r))
    print()
    print('  ⚠️ 判读：')
    print('     · 送转是价值中性的 → **正确口径下 dy 在送转除权日应基本连续（比值≈1）**')
    print('     · 若 L 的中位比值 ≈ (1+r) 而 C 的 ≈ 1 → **本地现行 adj 的方向是错的**')
    print('     · 本脚本只做检验，**不改任何研究口径**；是否修由主人决定')

    # ============================== 三、影响面量化 ==============================
    print()
    print('=' * 96)
    print('  三、影响面：现行口径把 dps 分子放大了多少（两组共用同一分母，比值即为 dy 比值）')
    print('=' * 96)
    li = loc.set_index(['code', 'date'])
    mod.ADJ_MODE = 'correct'
    cor = {}
    for dt, grp in li.groupby(level='date'):
        cs = grp.index.get_level_values('code').tolist()
        s = mod._yield_at(dt, cs)[0]
        for c in cs:
            cor[(c, dt)] = float(s.get(c, 0.0))
    mod.ADJ_MODE = ADJ_MODE_DEFAULT
    w2 = pd.DataFrame([(c, dt, float(v), cor.get((c, dt), 0.0))
                       for (c, dt), v in li['dps_ttm'].items()],
                      columns=['code', 'date', 'loc', 'cor'])
    nz = w2[(w2['loc'] > 0) | (w2['cor'] > 0)]
    bad = nz[(nz['loc'] - nz['cor']).abs() > 1e-9]
    r = (bad['loc'] / bad['cor']).replace([np.inf, -np.inf], np.nan).dropna()
    print('  有分红的 (股票, 除权日) 组合 {} 个'.format(len(nz)))
    print('  两口径不一致 {} 个（{:.2f}%）'.format(len(bad), len(bad) / max(len(nz), 1) * 100))
    if len(r):
        print('  现行/正确 的 dps 比值：中位 {:.3f} ｜ 均值 {:.3f} ｜ 90分位 {:.3f} ｜ 最大 {:.2f}'.format(
            float(r.median()), float(r.mean()), float(r.quantile(0.9)), float(r.max())))
        for thr in (1.2, 2.0, 5.0, 10.0):
            k = int((r > thr).sum())
            print('     比值 > {:>4.1f}× 的占 {:>6.2f}%  （{} 个）'.format(
                thr, k / max(len(nz), 1) * 100, k))
    print()
    print('  ⚠️ 这不是「选股名单会变多少」—— 那要修完之后跑对照回测才算数。')
    print('     但可以确定：**`≤10%` 上限会挡掉一部分被放大到离谱的标的**（尤其是高送转股），')
    print('     所以实际影响 < 这里的比例；具体多大必须实测。')

    # ============================== 四、聚宽环境兼容性静态检查 ==============================
    print()
    print('=' * 96)
    print('  四、聚宽环境兼容性（实测：聚宽回测 = 老 pandas 0.23 系 + Python 2 系）')
    print('=' * 96)
    src = open(os.path.join(ROOT, 'joinquant', 'jq_dividend.py'),
               encoding='utf-8').read()
    bad = py2_compat_scan(src)
    if not bad:
        print('  ✅ 未发现 f-string / 类型注解 / 标量字符串 Series 这三类已知会炸的写法')
    else:
        for ln, why, code in bad:
            print('  ❌ 第 {} 行：{}'.format(ln, why))
            print('       {}'.format(code))
    print()
    print('  依据（2026-09-17 聚宽实跑报错）：')
    print('    pandas/core/series.py:275 _sanitize_array -> maybe_cast_to_datetime')
    print("    -> np.dtype('未分类') -> UnicodeEncodeError: 'ascii' codec can't encode")
    print('  → 该栈只在 Py2 出现（Py3 下 np.dtype 对未知字符串抛 TypeError），')
    print('    且 series.py 的行号对应 pandas 0.23 系（2018 年）。')
    print()
    print('  三条必须守住的写法约束：')
    print('    ① 不用 f-string / 类型注解 / nonlocal（Py2 不支持）')
    print('    ② 不把标量字符串传给 pd.Series(..., index=...)（老 pandas 会拿它推 dtype）')
    print('    ③ 含非 ASCII 的 .format() 模板，参数必须是 ASCII 或 byte str ——')
    print('       「非 ASCII 模板 + unicode 参数」在 Py2 下抛 UnicodeDecodeError。')
    print('       聚宽返回的行业名就是中文 unicode → 涉及它的模板一律保持纯 ASCII。')
    return 0


def py2_compat_scan(src: str):
    """用 ast 精确扫三类「在聚宽老环境下会炸」的写法（比正则可靠）。

    ① `ast.JoinedStr` → f-string（Py2 不支持）
    ② 函数/参数上的 annotation、以及 `from __future__ import annotations`
    ③ `pd.Series(<字符串字面量>, ...)` → 老 pandas 会把标量字符串当 list-like 推 dtype
    """
    import ast
    out = []
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            out.append((getattr(node, 'lineno', 0), 'f-string（Py2 不支持）',
                        '<f-string>'))
        if isinstance(node, ast.ImportFrom) and node.module == '__future__':
            for a in node.names:
                if a.name == 'annotations':
                    out.append((node.lineno, 'from __future__ import annotations',
                                '（Py2 不需要也不支持）'))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                out.append((node.lineno, '函数返回类型注解', 'def {}'.format(node.name)))
            for a in list(getattr(node.args, 'args', [])) + \
                    list(getattr(node.args, 'kwonlyargs', [])):
                if getattr(a, 'annotation', None) is not None:
                    out.append((node.lineno, '参数类型注解', 'def {}'.format(node.name)))
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.append((node.lineno, '变量类型注解', node.target.id))
        if (isinstance(node, ast.Call)
                and getattr(getattr(node, 'func', None), 'attr', '') == 'Series'
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            out.append((node.lineno, 'pd.Series(标量字符串, ...) —— 老 pandas 会崩',
                        repr(node.args[0].value)))
    return sorted(set(out))


if __name__ == '__main__':
    sys.exit(main())
