"""卡片管理 —— 赛题场景 4（卡申请 / 交易限制解锁 / 挂失解挂）。

四个能力：
  * 查询卡片（绿色，自动执行）
  * 申请虚拟卡（黄色，用户确认）—— 赛题第 33 行明确列为黄色
  * 挂失（红色，强验证）
  * 解挂（红色，强验证）

★ 为什么「挂失」是红色，尽管它「只是把卡停掉」：
   挂失不产生资金变动，看着比转账轻，但它是一次不可逆的服务阻断——
   把受害人的卡锁死，本身就是一种攻击（拒绝服务 + 逼用户走人工渠道再实施诈骗）。
   所以它必须过人脸/短信，不能一句话就执行。

★ 为什么「解挂」也是红色：
   挂失是**降低**风险暴露，解挂是**抬高**风险暴露。
   凡是提高风险暴露的操作，处理标准只能更严，不能更松——
   这正是本项目「风险等级只升不降」在业务侧的同一条原则。
"""

from __future__ import annotations

import sqlite3

from pydantic import BaseModel, Field

from app.bank.db import (
    CARD_KIND_CN,
    find_cards,
    get_card,
    insert_card,
    list_cards,
    set_card_status,
)
from app.schemas import RiskLevel, ToolResult, UserContext
from app.tools.base import ToolMeta, register

FROZEN = "frozen"      # 已挂失
ACTIVE = "active"

# 虚拟卡默认单日限额：1000 元，与转账的黄色阈值同口径。
# ★ 表述层要用同一个常量（确认提示里必须显示真实限额），
#   所以它只能是模块级常量，不能在两个文件里各写一个 100_000。
DEFAULT_VIRTUAL_DAILY_CENTS = 100_000


# ------------------------------------------------------------------ 解析

def _near_miss(conn: sqlite3.Connection, user_id: str, keyword: str):
    """尾号只差一位时给个提示，但**绝不替用户决定**。"""
    if not (keyword.isdigit() and len(keyword) == 4):
        return None
    for row in list_cards(conn, user_id):
        last4 = str(row["last4"] or "")
        if len(last4) == 4 and sum(a != b for a, b in zip(last4, keyword)) == 1:
            return row
    return None


def _resolve(conn: sqlite3.Connection, user_id: str, keyword: str):
    """把「尾号2233」「信用卡」解析成一张确定的卡。

    返回 (card_row | None, 错误信息 | None)。找不到或有歧义都算错误——
    在卡片操作上猜错的代价是把用户的卡挂失掉，绝不能猜。
    """
    rows = find_cards(conn, user_id, keyword)
    if not rows:
        near = _near_miss(conn, user_id, keyword)
        if near is not None:
            kind = CARD_KIND_CN.get(near["kind"], near["kind"])
            return None, (
                f"没有尾号 {keyword} 的卡。你指的是不是 {kind}（尾号{near['last4']}）？"
                f"如果是，请说「尾号{near['last4']}」——我不会自己替你换一张卡："
                f"在卡片操作上猜错，代价是把另一张卡锁死。"
            )
        return None, f"没有找到「{keyword}」对应的卡"
    if len(rows) > 1:
        names = "、".join(f"{CARD_KIND_CN.get(r['kind'], r['kind'])}(尾号{r['last4']})" for r in rows)
        return None, f"「{keyword}」匹配到多张卡（{names}），请说明是哪一张"
    return rows[0], None


def _precheck_card(
    conn: sqlite3.Connection, user_id: str, args: BaseModel, want_frozen: bool
) -> str | None:
    card, err = _resolve(conn, user_id, getattr(args, "card"))
    if err:
        return err
    assert card is not None
    if want_frozen and card["status"] == FROZEN:
        return f"该卡（尾号{card['last4']}）已经处于挂失状态，无需重复挂失"
    if not want_frozen and card["status"] == ACTIVE:
        return f"该卡（尾号{card['last4']}）当前状态正常，没有可解除的限制"
    return None


# ------------------------------------------------------------------ 查询

class QueryCardsArgs(BaseModel):
    include_frozen: bool = Field(default=True, description="是否包含已挂失的卡")


def _query_cards(conn: sqlite3.Connection, ctx: UserContext, args: QueryCardsArgs) -> ToolResult:
    rows = [dict(r) for r in list_cards(conn, ctx.user_id)]
    if not args.include_frozen:
        rows = [r for r in rows if r["status"] == ACTIVE]
    for r in rows:
        r["kind_cn"] = CARD_KIND_CN.get(r["kind"], r["kind"])
    return ToolResult(
        ok=True,
        tool="query_cards",
        data={"items": rows, "count": len(rows),
              "frozen_count": sum(1 for r in rows if r["status"] != ACTIVE)},
    )


register(
    ToolMeta(
        name="query_cards",
        label="查询卡片",
        base_level=RiskLevel.GREEN,
        is_fund_movement=False,
        args_model=QueryCardsArgs,
        handler=_query_cards,
        description="列出本人名下的卡及其状态、限额、卡号后四位。",
    )
)


# ------------------------------------------------------------------ 申请虚拟卡

class ApplyVirtualArgs(BaseModel):
    # 默认单日 1000 元，与转账的黄色阈值同口径：虚拟卡是用来「小额网购」的，
    # 默认值本身就是一道风控，而不是让用户自己去改一个更大的数。
    daily_limit_cents: int = Field(default=DEFAULT_VIRTUAL_DAILY_CENTS, ge=0, le=500_000)
    brand: str = Field(default="银联", max_length=16)


def _apply_virtual_card(
    conn: sqlite3.Connection, ctx: UserContext, args: ApplyVirtualArgs
) -> ToolResult:
    row = insert_card(
        conn,
        user_id=ctx.user_id,
        kind="virtual",
        brand=args.brand,
        daily_limit_cents=args.daily_limit_cents,
    )
    return ToolResult(
        ok=True,
        tool="apply_virtual_card",
        data={
            "card_id": row["id"],
            "kind_cn": CARD_KIND_CN["virtual"],
            "brand": row["brand"],
            "last4": row["last4"],
            "daily_limit_cents": row["daily_limit_cents"],
            "status": row["status"],
        },
    )


register(
    ToolMeta(
        name="apply_virtual_card",
        label="申请虚拟卡",
        base_level=RiskLevel.YELLOW,
        is_fund_movement=False,
        args_model=ApplyVirtualArgs,
        handler=_apply_virtual_card,
        description="为本人开立一张虚拟卡，用于小额网络支付，单日限额可指定。",
    )
)


# ------------------------------------------------------------------ 挂失 / 解挂

class CardRefArgs(BaseModel):
    card: str = Field(min_length=1, max_length=32, description="卡号后四位或卡种，如「2233」「信用卡」")
    reason: str = Field(default="", max_length=64)


class UnfreezeArgs(BaseModel):
    card: str = Field(min_length=1, max_length=32, description="卡号后四位或卡种")


def _freeze_card(conn: sqlite3.Connection, ctx: UserContext, args: CardRefArgs) -> ToolResult:
    card, err = _resolve(conn, ctx.user_id, args.card)
    if err or card is None:
        return ToolResult(ok=False, tool="freeze_card", error=err or "卡片解析失败")
    # 防御性复核：策略引擎已判过归属，这里再查一次
    if get_card(conn, ctx.user_id, card["id"]) is None:
        return ToolResult(ok=False, tool="freeze_card", error="该卡不属于当前用户，已拒绝")

    set_card_status(conn, ctx.user_id, card["id"], FROZEN, args.reason or "用户主动挂失")
    return ToolResult(
        ok=True,
        tool="freeze_card",
        data={
            "last4": card["last4"],
            "kind_cn": CARD_KIND_CN.get(card["kind"], card["kind"]),
            "brand": card["brand"],
            "reason": args.reason or "用户主动挂失",
            "status": FROZEN,
        },
    )


def _unfreeze_card(conn: sqlite3.Connection, ctx: UserContext, args: UnfreezeArgs) -> ToolResult:
    card, err = _resolve(conn, ctx.user_id, args.card)
    if err or card is None:
        return ToolResult(ok=False, tool="unfreeze_card", error=err or "卡片解析失败")
    if get_card(conn, ctx.user_id, card["id"]) is None:
        return ToolResult(ok=False, tool="unfreeze_card", error="该卡不属于当前用户，已拒绝")

    set_card_status(conn, ctx.user_id, card["id"], ACTIVE, None)
    return ToolResult(
        ok=True,
        tool="unfreeze_card",
        data={
            "last4": card["last4"],
            "kind_cn": CARD_KIND_CN.get(card["kind"], card["kind"]),
            "brand": card["brand"],
            "status": ACTIVE,
        },
    )


register(
    ToolMeta(
        name="freeze_card",
        label="卡片挂失",
        base_level=RiskLevel.RED,
        is_fund_movement=False,
        args_model=CardRefArgs,
        handler=_freeze_card,
        description="将本人名下某张卡置为挂失状态，立即阻断该卡交易。",
        precheck=lambda c, u, a: _precheck_card(c, u, a, want_frozen=True),
    )
)

register(
    ToolMeta(
        name="unfreeze_card",
        label="卡片解挂",
        base_level=RiskLevel.RED,
        is_fund_movement=False,
        args_model=UnfreezeArgs,
        handler=_unfreeze_card,
        description="解除本人名下某张卡的挂失/锁定，恢复交易能力。",
        precheck=lambda c, u, a: _precheck_card(c, u, a, want_frozen=False),
    )
)
