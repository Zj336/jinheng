"""转账 —— 唯一同时覆盖绿/黄/红的工具，也是权限分级的演示主战场。

执行器只做「原子动作」，不做任何风险判断。
风险判断在前面的策略引擎里已经完成，这里再做一次防御性校验（纵深防御）。
"""

from __future__ import annotations

import sqlite3
from datetime import date

from pydantic import BaseModel, Field

from app.bank.db import (
    daily_transferred_cents,
    find_contacts,
    get_account,
    get_contact,
    new_id,
    now_iso,
)
from app.schemas import RiskLevel, ToolResult, UserContext
from app.tools.base import ToolMeta, register


class TransferArgs(BaseModel):
    to: str = Field(min_length=1, max_length=64, description="收款人：姓名 / 备注 / 联系人ID")
    amount_cents: int = Field(gt=0, le=100_000_000, description="金额，单位分")
    memo: str = Field(default="", max_length=64)
    schedule_date: str | None = Field(
        default=None, description="定时转账的日期 YYYY-MM-DD；为空表示立即执行"
    )


def resolve_contact(conn: sqlite3.Connection, user_id: str, keyword: str):
    """把用户口头说的收款人解析成联系人记录。

    只在**本人**联系人范围内匹配——这是越权防线的一部分。
    返回 (contact_row | None, candidates)：
      candidates 长度 > 1 表示有歧义，必须让用户澄清，不能猜。
    """
    if keyword.startswith("c_"):
        row = get_contact(conn, user_id, keyword)
        return (row, [row] if row else [])
    rows = find_contacts(conn, user_id, keyword)
    if len(rows) == 1:
        return (rows[0], rows)
    return (None, rows)


def _transfer(conn: sqlite3.Connection, ctx: UserContext, args: TransferArgs) -> ToolResult:
    contact, candidates = resolve_contact(conn, ctx.user_id, args.to)
    if contact is None:
        err = "收款人有歧义，请澄清" if len(candidates) > 1 else f"未找到收款人「{args.to}」"
        return ToolResult(ok=False, tool="transfer", error=err)

    acc = get_account(conn, ctx.user_id, "current")
    if acc is None:
        return ToolResult(ok=False, tool="transfer", error="付款账户异常")

    # 防御性复核：策略引擎已经判过，这里不重复判风险等级，
    # 只保证「不可能转出超过余额的钱」这一物理事实。
    if args.amount_cents > acc["balance_cents"]:
        return ToolResult(ok=False, tool="transfer", error="余额不足")

    scheduled = args.schedule_date is not None

    # ★ 定时转账 = 预约，不是「现在扣钱、以后到账」。
    #   修之前：`scheduled` 只改了台账的一个状态字段，余额当场扣掉、
    #   流水按 now 落库、台账 status='scheduled'，而所有限额只统计 'executed'
    #   ——钱已经走了却不占额度，相当于给拆单规避开了一扇侧门。
    #   现在：预约**不动钱、不写交易流水**，只登记一条 status='scheduled' 的台账，
    #   并把它的 ts 记成**执行日**，于是它占用的是执行日那一天（当月也照算）的额度。
    if scheduled:
        try:
            due = date.fromisoformat(args.schedule_date or "")
        except ValueError:
            return ToolResult(ok=False, tool="transfer", error="预约日期格式应为 YYYY-MM-DD")
        if due < date.today():
            return ToolResult(ok=False, tool="transfer", error="预约日期不能早于今天")
        ledger_ts = f"{due.isoformat()}T00:00:00"
        ledger_status = "scheduled"
    else:
        ledger_ts = now_iso()
        ledger_status = "executed"
        conn.execute(
            "UPDATE accounts SET balance_cents = balance_cents - ? WHERE id=?",
            (args.amount_cents, acc["id"]),
        )
        conn.execute(
            "INSERT INTO transactions(id,account_id,user_id,ts,direction,amount_cents,"
            "counterparty,category,memo) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                new_id("t_"),
                acc["id"],
                ctx.user_id,
                now_iso(),
                "out",
                args.amount_cents,
                contact["name"],
                "转账",
                args.memo or "转账",
            ),
        )

    conn.execute(
        "INSERT INTO transfer_ledger(id,user_id,ts,amount_cents,contact_id,counterparty,"
        "status,decision_level,trace_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            new_id("l_"),
            ctx.user_id,
            ledger_ts,
            args.amount_cents,
            contact["id"],
            contact["name"],
            ledger_status,
            "",
            ctx.trace_id,
        ),
    )

    return ToolResult(
        ok=True,
        tool="transfer",
        data={
            "to_name": contact["name"],
            "to_bank": contact["bank"],
            "to_account_masked": "****" + (contact["account_no"] or "")[-4:],
            "amount_cents": args.amount_cents,
            "memo": args.memo,
            "scheduled": scheduled,
            "schedule_date": args.schedule_date,
            "daily_total_after_cents": daily_transferred_cents(conn, ctx.user_id),
            # 预约日对应的已占用额度（含本条），让用户看得见「那天还剩多少」
            "scheduled_day_used_cents": daily_transferred_cents(
                conn, ctx.user_id, args.schedule_date
            ) if scheduled else None,
        },
    )


register(
    ToolMeta(
        name="transfer",
        label="转账",
        # 基级给黄色（小额默认），大额由策略引擎按日累计升到红色。
        base_level=RiskLevel.YELLOW,
        is_fund_movement=True,
        args_model=TransferArgs,
        handler=_transfer,
        description="向本人联系人发起转账，支持即时与定时。",
        resolves_contact=True,
        min_kyc=1,
    )
)
