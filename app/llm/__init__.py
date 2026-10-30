"""LLM 适配层。只负责「理解」和「规划」，永远不碰执行。"""

from __future__ import annotations

from app.config import settings
from app.llm.base import LLMAdapter, Proposal

_ADAPTER: LLMAdapter | None = None


def get_adapter() -> LLMAdapter:
    """按配置返回适配器。mock 是默认值，保证零依赖可跑。"""
    global _ADAPTER
    if _ADAPTER is not None:
        return _ADAPTER

    if settings.llm_mode == "mock":
        from app.llm.mock import MockLLM

        _ADAPTER = MockLLM()
    else:
        from app.llm.openai_compat import OpenAICompatLLM

        _ADAPTER = OpenAICompatLLM()
    return _ADAPTER


def set_adapter(adapter: LLMAdapter) -> None:
    """测试注入用。"""
    global _ADAPTER
    _ADAPTER = adapter


__all__ = ["LLMAdapter", "Proposal", "get_adapter", "set_adapter"]
