"""面板构建：将多标的标准行情构建成横截面面板(date × code)。"""
from typing import Dict, List, Optional

import pandas as pd

from .store import DataStore, classify_code

_OCCL_FIELDS = ["open", "high", "low", "close", "volume", "rate"]


class Panel:
    def __init__(self, fields: Dict[str, pd.DataFrame], codes, categories):
        self.fields = fields
        self.codes = list(codes)
        self.categories = categories

    def get(self, field: str) -> Optional[pd.DataFrame]:
        return self.fields.get(field)

    @property
    def dates(self) -> List[str]:
        return list(self.fields["close"].index)

    def __len__(self):
        return len(self.codes)

    def slice(self, start=None, end=None):
        """按日期截断面板（用于因子 warmup 预热后截取回测区间）。"""
        close = self.fields.get("close")
        if close is None:
            return self
        idx = close.index
        if start:
            idx = idx[idx >= str(start)]
        if end:
            idx = idx[idx <= str(end)]
        fields = {f: df.reindex(idx) for f, df in self.fields.items()}
        return Panel(fields, self.codes, self.categories)


def build_panel(store: DataStore, codes, start=None, end=None,
                fields: List[str] = None, align="close") -> Panel:
    fields = fields or _OCCL_FIELDS
    frames = {}
    for code in codes:
        frames[code] = store.read(code, start=start, end=end)

    anchor = pd.DataFrame({c: f["close"] for c, f in frames.items()})
    anchor = anchor.dropna().sort_index()
    common_dates = anchor.index

    tables = {}
    for f in fields:
        t = pd.DataFrame({c: frames[c][f] for c in codes if f in frames[c].columns})
        if t.empty:
            continue
        t = t.loc[common_dates]
        t = t.apply(pd.to_numeric, errors="coerce")
        tables[f] = t

    categories = {c: classify_code(c) for c in codes}
    return Panel(tables, codes, categories)