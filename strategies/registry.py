"""策略注册表。"""
from .base import Strategy
from .builtin import (MomentumStrategy, MeanReversionStrategy, CrossMovingStrategy,
                      MultiFactorStrategy, LianbanLeadStrategy, EtfRotationStrategy,
                      TechOffensiveStrategy, BigOrderEtfStrategy)

STRATEGIES = {
    "momentum": MomentumStrategy,
    "etf_rotation": EtfRotationStrategy,
    "mean_reversion": MeanReversionStrategy,
    "cross_moving": CrossMovingStrategy,
    "multifactor": MultiFactorStrategy,
    "lianban_lead": LianbanLeadStrategy,
    "tech_offensive": TechOffensiveStrategy,
    # A 路线：ETF 轮动 + 精灵大单资金流（需 dataprovider.altdata 注入字段）
    "bigorder_etf": BigOrderEtfStrategy,
}


def register_strategy(name: str, cls):
    STRATEGIES[name] = cls


def list_strategies() -> list:
    return list(STRATEGIES.keys())


def create_strategy(name: str, **params) -> Strategy:
    if name not in STRATEGIES:
        raise KeyError("未知策略: {}，可用: {}".format(name, list_strategies()))
    return STRATEGIES[name](**params)