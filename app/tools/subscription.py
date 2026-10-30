"""订阅 / 代扣管理 —— 赛题场景 5。

两个能力：
  * 查询订阅（绿色，自动执行）
  * 取消订阅（黄色，用户确认）

★ 为什么「取消订阅」只是黄色、而转账是黄色起步：
   赛题第 33 行明确把「订阅查询/取消」放在黄色。
   我们的判断依据不是照抄赛题，而是同一个判据——**能不能不可逆地动到钱**。
   取消订阅只停掉未来的扣款，不产生即时资金变动，所以黄色。

★ 未实现、也如实标注的：订阅的**自动异常识别**（长期未使用仍在扣费、
   扣款金额突增）。查询里已经给了「只看月扣款 ≥100 元」这个筛子，
   但系统不会自己判定「这笔扣贵了」——那需要消费行为基线，目前没有。
   所以这里不写「会自动升级到红色」：不存在的功能不该出现在文档里。
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.bank.db import active_subscriptions, find_subscriptions, get_subscription, set_subscription_status
from app.schemas import RiskLevel, ToolResult, UserContext
from app.tools.base import ToolMeta, register


# ------------------------------------------------------------------ 查询

class QuerySubsArgs(BaseModel):
    only_expensive: bool = Field(default=False, description="只看月扣款 ≥ 100 元的")


def _query_subscriptions(
    conn: sqlite3.Connection, ctx: UserContext, args: QuerySubsArgs
) -> ToolResult:
    rows = [dict(r) for r in active_subscriptions(conn, ctx.user_id)]
    if args.only_expensive:
        rows = [r for r in rows if r["amount_cents"] >= 10_000]
    monthly = sum(r["amount_cents"] for r in rows if r["period"] == "monthly")
    yearly = sum(r["amount_cents"] for r in rows if r["period"] == "yearly")

    # ★ 续费提醒是「拉取式」的：不问不推，问就把未来 7 天要扣的钱算清楚。
    #   为什么不主动推：我们没有推送通道，也不该为了像样就给演示塞一个假的推送。
    #   「未来 7 天将自动扣费 ¥X」——这句话用户才真正需要，因为它回答的是
    #   「我这张卡下周会不会被扣穿」，而不是「我一共有几个会员」。
    today = date.today()
    upcoming: list[dict] = []
    for r in rows:
        try:
            due = date.fromisoformat(str(r.get("next_charge_date") or ""))
        except ValueError:
            continue  # 日期缺失/格式不对就不猜，"下次扣款 —" 已经说明了
        days_left = (due - today).days
        if 0 <= days_left <= 7:
            upcoming.append(
                {
                    "merchant": r["merchant"],
                    "amount_cents": r["amount_cents"],
                    "date": due.isoformat(),
                    "days_left": days_left,
                }
            )
    upcoming.sort(key=lambda x: (x["days_left"], -x["amount_cents"]))

    return ToolResult(
        ok=True,
        tool="query_subscriptions",
        data={
            "items": rows,
            "count": len(rows),
            "monthly_total_cents": monthly,
            "yearly_total_cents": yearly,
            # 折算成年支出，方便用户直观感受「小钱变大钱」
            "annualized_cents": monthly * 12 + yearly,
            "upcoming_7d": upcoming,
            "upcoming_7d_cents": sum(x["amount_cents"] for x in upcoming),
        },
    )


register(
    ToolMeta(
        name="query_subscriptions",
        label="查询订阅代扣",
        base_level=RiskLevel.GREEN,
        is_fund_movement=False,
        args_model=QuerySubsArgs,
        handler=_query_subscriptions,
        description="列出本人当前生效的订阅与代扣，并折算月度/年度支出。",
    )
)


# ------------------------------------------------------------------ 取消

class CancelSubArgs(BaseModel):
    merchant: str = Field(min_length=1, max_length=40, description="订阅商户名，可模糊匹配")
    period: Literal["immediate", "at_period_end"] = Field(
        default="at_period_end", description="immediate 立即停 / at_period_end 到期不续"
    )


def _precheck_cancel(
    conn: sqlite3.Connection, user_id: str, args: CancelSubArgs
) -> str | None:
    """在让用户确认之前就判断「退得掉吗」。"""
    candidates = find_subscriptions(conn, user_id, args.merchant)
    if not candidates:
        return f"没有找到名为「{args.merchant}」的有效订阅"
    if len(candidates) > 1:
        names = "、".join(c["merchant"] for c in candidates)
        return f"「{args.merchant}」匹配到多个订阅（{names}），请说明是哪一个"
    return None


def _cancel_subscription(
    conn: sqlite3.Connection, ctx: UserContext, args: CancelSubArgs
) -> ToolResult:
    candidates = find_subscriptions(conn, ctx.user_id, args.merchant)
    if not candidates:
        return ToolResult(
            ok=False, tool="cancel_subscription",
            error=f"没有找到名为「{args.merchant}」的有效订阅",
        )
    if len(candidates) > 1:
        names = "、".join(c["merchant"] for c in candidates)
        return ToolResult(
            ok=False, tool="cancel_subscription",
            error=f"「{args.merchant}」匹配到多个订阅（{names}），请说明是哪一个",
        )

    sub = candidates[0]
    # 再查一遍归属：与 policy 对转账做的二次确认同一条思路，
    # 防止将来有人改 find_subscriptions 时把越权防线拆掉。
    if get_subscription(conn, ctx.user_id, sub["id"]) is None:
        return ToolResult(
            ok=False, tool="cancel_subscription",
            error="该订阅不属于当前用户，已拒绝",
        )

    set_subscription_status(conn, ctx.user_id, sub["id"], "cancelled")
    return ToolResult(
        ok=True,
        tool="cancel_subscription",
        data={
            "merchant": sub["merchant"],
            "amount_cents": sub["amount_cents"],
            "period": sub["period"],
            "next_charge_date": sub["next_charge_date"],
            "cancel_mode": args.period,
            "saved_annual_cents": sub["amount_cents"] * 12 if sub["period"] == "monthly"
            else sub["amount_cents"],
        },
    )


register(
    ToolMeta(
        name="cancel_subscription",
        label="取消订阅",
        base_level=RiskLevel.YELLOW,
        is_fund_movement=False,
        args_model=CancelSubArgs,
        handler=_cancel_subscription,
        description="取消本人在某商户的订阅/代扣，执行前需用户确认。",
        precheck=_precheck_cancel,
    )
)
