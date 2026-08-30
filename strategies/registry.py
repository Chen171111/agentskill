"""策略注册表。"""
from .base import Strategy
from .builtin import (MomentumStrategy, MeanReversionStrategy, CrossMovingStrategy,
                      MultiFactorStrategy, LianbanLeadStrategy, EtfRotationStrategy)

STRATEGIES = {
    "momentum": MomentumStrategy,
    "etf_rotation": EtfRotationStrategy,
    "mean_reversion": MeanReversionStrategy,
    "cross_moving": CrossMovingStrategy,
    "multifactor": MultiFactorStrategy,
    "lianban_lead": LianbanLeadStrategy,
}


def register_strategy(name: str, cls):
    STRATEGIES[name] = cls


def list_strategies() -> list:
    return list(STRATEGIES.keys())


def create_strategy(name: str, **params) -> Strategy:
    if name not in STRATEGIES:
        raise KeyError("未知策略: {}，可用: {}".format(name, list_strategies()))
    return STRATEGIES[name](**params)