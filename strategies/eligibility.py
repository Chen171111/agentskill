"""Shared eligibility applied after momentum-family selection in both entries."""
from math import isfinite


def filter_positive_momentum(weights, factors, date):
    """Keep finite, strictly positive UNROUNDED 20-day momentum.

    None means no rebalance and is preserved. Missing observations cannot prove
    eligibility and are excluded. Weights are retained without redistribution.
    Callers opt in by strategy; mean reversion does not use this rule.
    """
    if weights is None:
        return None
    momentum = factors.get('momentum20')
    if momentum is None or date not in momentum.index:
        return {}
    row = momentum.loc[date]
    result = {}
    for code, weight in weights.items():
        value = row.get(code)
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if isfinite(value) and value > 0:
            result[code] = weight
    return result
