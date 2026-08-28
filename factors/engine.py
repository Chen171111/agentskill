"""因子计算引擎：在面板上逐标的计算各因子。"""
from typing import Dict, List

import pandas as pd

from .registry import FACTOR_FUNCS

_NEED_FIELDS = {
    "rsi": ["close"], "macd_hist": ["close"], "bias20": ["close"],
    "sma_gap": ["close"], "natr": ["high", "low", "close"], "boll_pos": ["close"],
    "momentum20": ["close"], "vol_ratio": ["volume"], "zt_daily": ["close"],
    "lianban": ["close"],
}


def compute_factors(panel, factor_names: List[str] = None) -> Dict[str, pd.DataFrame]:
    factor_names = factor_names or list(FACTOR_FUNCS.keys())
    out = {}
    dates = panel.dates
    for name in factor_names:
        if name not in FACTOR_FUNCS:
            continue
        fn = FACTOR_FUNCS[name]
        cols = {}
        for f in _NEED_FIELDS.get(name, ["close"]):
            df = panel.get(f)
            if df is None:
                raise KeyError("面板缺少字段: {}".format(f))
            cols[f] = df
        per_code = {}
        for code in panel.codes:
            px = {f: df[code] for f, df in cols.items()}
            per_code[code] = fn(px)
        out[name] = pd.DataFrame(per_code, index=dates).sort_index()
    return out