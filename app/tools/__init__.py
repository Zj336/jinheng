"""导入即注册。新增工具只要在下面加一行 import。"""

from app.tools import account, analysis, card, subscription, transfer, wealth  # noqa: F401
from app.tools.base import REGISTRY, ToolMeta, get_tool, tool_names

__all__ = ["REGISTRY", "ToolMeta", "get_tool", "tool_names"]
