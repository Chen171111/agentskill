"""Deterministic transaction fees shared by order sizing and confirmation."""
import math
import config


def estimate_fee(notional, code, is_sell=False, cost=None):
    from dataprovider.store import classify_code
    value = float(notional)
    if not math.isfinite(value) or value < 0:
        raise ValueError("成交金额必须是有限非负数")
    if value == 0:
        return 0.0
    terms = config.TRADING_COST if cost is None else cost
    fee = max(value * float(terms.get("commission_rate", .0005)),
              float(terms.get("min_commission", 5.)))
    if is_sell and classify_code(code) == "stock":
        fee += value * float(terms.get("sell_tax_rate", .001))
    return fee
