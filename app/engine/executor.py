"""确定性执行器。

只做一件事：把已经通过裁决的 Step 变成一次工具调用。
这里不再有任何分支判断——风险研判已在 policy 层完成。
"""

from __future__ import annotations

import sqlite3

from app.schemas import Step, ToolResult, UserContext
from app.tools.base import get_tool


def execute_step(conn: sqlite3.Connection, ctx: UserContext, step: Step) -> ToolResult:
    meta = get_tool(step.tool)
    if meta is None:  # policy 已拦过，这里是纵深防御
        return ToolResult(ok=False, tool=step.tool, step_id=step.id, error="工具未注册")

    args = meta.args_model.model_validate(step.args)
    result = meta.handler(conn, ctx, args)
    result.step_id = step.id
    result.tool = meta.name
    return result
