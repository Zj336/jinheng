"""工具注册表。

每个工具必须显式声明三件事，策略引擎只信这三件事、不信 LLM 的话：
  * base_level      风险基级（绿/黄/红）
  * is_fund_movement 是否涉及资金变动（涉及则要做额度累计）
  * args_model      参数 Schema（pydantic 强校验，拒绝自由文本注入）
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel

from app.schemas import DecisionOutcome, RiskLevel, ToolResult, UserContext


# 前置解析钩子：给定 (conn, user_id, args)，返回 None 表示可继续，
# 返回字符串表示「这个操作根本不可能成功」的原因。
# 由策略引擎在裁决前调用，属于纯代码、不接触自然语言。
Precheck = Callable[[sqlite3.Connection, str, BaseModel], "str | None"]


@dataclass(frozen=True)
class ToolMeta:
    name: str
    label: str                      # 中文名，表述层用
    base_level: RiskLevel
    is_fund_movement: bool
    args_model: type[BaseModel]
    handler: Callable[[sqlite3.Connection, UserContext, BaseModel], ToolResult]
    description: str = ""
    # ★ 为什么需要它：没有前置解析，系统会先让用户做多因子认证、
    #   认证完才发现目标不存在——既浪费一次人脸/短信，也让用户觉得系统不靠谱。
    precheck: Precheck | None = None
    # ★ 是否需要在策略引擎里解析「收款人」。
    #   曾经这里写的是 isinstance(args, TransferArgs)，等于把「转账」这个具体工具
    #   硬编码进了策略引擎——再加一个资金类工具（理财申购）就会直接断言崩溃。
    #   现在改成由工具自己声明，引擎对工具一无所知。
    resolves_contact: bool = False
    # 该工具要求的最低 KYC 等级。理财申购要求 2 级（做过风险测评），转账 1 级。
    min_kyc: int = 1


REGISTRY: dict[str, ToolMeta] = {}


def register(meta: ToolMeta) -> ToolMeta:
    if meta.name in REGISTRY:
        raise ValueError(f"工具重复注册: {meta.name}")
    REGISTRY[meta.name] = meta
    return meta


def get_tool(name: str) -> ToolMeta | None:
    return REGISTRY.get(name)


def tool_names() -> list[str]:
    return sorted(REGISTRY)


# 绿/黄/红 与执行策略的映射。
# ★ 只有这里定义映射关系，且它是纯代码常量，不可由运行时数据、更不可由 LLM 改变。
LEVEL_TO_OUTCOME: dict[RiskLevel, DecisionOutcome] = {
    RiskLevel.GREEN: DecisionOutcome.ALLOW,
    RiskLevel.YELLOW: DecisionOutcome.CONFIRM,
    RiskLevel.RED: DecisionOutcome.MFA,
}

LEVEL_ORDER = [RiskLevel.GREEN, RiskLevel.YELLOW, RiskLevel.RED]


def escalate(level: RiskLevel, steps: int = 1) -> RiskLevel:
    """只升不降。整个系统里不存在降低风险等级的代码路径。"""
    i = LEVEL_ORDER.index(level)
    return LEVEL_ORDER[min(i + steps, len(LEVEL_ORDER) - 1)]
