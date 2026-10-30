"""账户查询类工具 —— 全部属于「绿色：自动执行」区。"""

from __future__ import annotations

import sqlite3
from typing import Literal

from pydantic import BaseModel, Field

from app.bank.db import get_account, recent_transactions
from app.schemas import RiskLevel, ToolResult, UserContext
from app.tools.base import ToolMeta, register


class QueryBalanceArgs(BaseModel):
    account_kind: Literal["current", "savings"] = Field(
        default="current", description="账户类型：current 活期 / savings 储蓄"
    )


def _query_balance(
    conn: sqlite3.Connection, ctx: UserContext, args: QueryBalanceArgs
) -> ToolResult:
    acc = get_account(conn, ctx.user_id, args.account_kind)
    if acc is None:
        return ToolResult(ok=False, tool="query_balance", error="账户不存在或已冻结")
    return ToolResult(
        ok=True,
        tool="query_balance",
        data={
            "account_kind": args.account_kind,
            "balance_cents": acc["balance_cents"],
            "currency": acc["currency"],
        },
    )


register(
    ToolMeta(
        name="query_balance",
        label="查询余额",
        base_level=RiskLevel.GREEN,
        is_fund_movement=False,
        args_model=QueryBalanceArgs,
        handler=_query_balance,
        description="查询本人在指定账户下的实时余额。",
    )
)


class QueryTransactionsArgs(BaseModel):
    days: int = Field(default=30, ge=1, le=90, description="回溯天数")
    category: str | None = Field(default=None, description="可选，按消费类目过滤")


def _query_transactions(
    conn: sqlite3.Connection, ctx: UserContext, args: QueryTransactionsArgs
) -> ToolResult:
    rows = recent_transactions(conn, ctx.user_id, args.days)
    items = [dict(r) for r in rows]
    if args.category:
        items = [r for r in items if (r.get("category") or "") == args.category]
    return ToolResult(
        ok=True,
        tool="query_transactions",
        data={"days": args.days, "count": len(items), "items": items},
    )


register(
    ToolMeta(
        name="query_transactions",
        label="查询交易流水",
        base_level=RiskLevel.GREEN,
        is_fund_movement=False,
        args_model=QueryTransactionsArgs,
        handler=_query_transactions,
        description="查询本人在指定时间窗口内的交易流水。",
    )
)
