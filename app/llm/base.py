"""LLM 接口定义。

★ 这是整个系统的信任边界。
   LLM 能做的：把自然语言变成 Intent，把 Intent 变成 Plan。
   LLM 不能做的：决定风险等级、决定是否执行、决定对用户说什么结果。
后两件事分别由 policy.py 和 render.py 独占。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.engine.guard import InputEnvelope
from app.schemas import Intent, Plan


@dataclass
class Proposal:
    """LLM 的完整产出物。注意它只包含「打算做什么」，不包含「做成了什么」。"""

    intents: list[Intent] = field(default_factory=list)
    plan: Plan = field(default_factory=Plan)
    clarifications: list[str] = field(default_factory=list)
    raw: str = ""


class LLMAdapter(ABC):
    name: str = "base"

    @abstractmethod
    def propose(self, envelope: InputEnvelope) -> Proposal:
        """把一封（已隔离的）用户输入解析成意图与计划。不得产生任何副作用。"""
