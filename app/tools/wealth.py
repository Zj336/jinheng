"""理财操作 —— 赛题场景 3（理财产品查询 / 申购）。

两个能力：
  * 查询在售理财（绿色，自动执行）
  * 申购（红色，强验证 + 资金变动）

★ 「申购」为什么必须是红色且永远不做黄色：
   转账的黄色档是「小额可确认」，但理财申购一旦成交，资金进入封闭期，
   在开放赎回前取不出来。它的**可逆性**比同等金额的转账差得多，
   所以不能因为金额小就降级。风险分级看的不是数字大小，是「撤得回来吗」。

★ 申购为什么不占转账的当日额度（也不该占）：
   两者是不同性质的资金出口。若混在一起累计，用户上午买了 900 元理财，
   下午连 100 元的正常转账都会被判成超额——那不是风控，那是故障。
   这正是本次改动把 `resolves_contact` 做进工具声明里的直接原因。
"""

from __future__ import annotations

import sqlite3

from pydantic import BaseModel, Field

from app.bank.db import (
    find_wealth_products,
    get_account,
    get_wealth_product,
    insert_wealth_order,
    list_wealth_orders,
    list_wealth_products,
    new_id,
    now_iso,
)
from app.schemas import RiskLevel, ToolResult, UserContext
from app.tools.base import ToolMeta, register


def _resolve_product(conn: sqlite3.Connection, keyword: str):
    """返回 (product | None, 错误信息 | None)。歧义一律不猜。"""
    rows = find_wealth_products(conn, keyword)
    if not rows:
        return None, f"没有找到名为「{keyword}」的在售理财产品"
    if len(rows) > 1:
        names = "、".join(r["name"] for r in rows)
        return None, f"「{keyword}」匹配到多只产品（{names}），请说明是哪一只"
    return rows[0], None


# ------------------------------------------------------------------ 查询

class QueryProductsArgs(BaseModel):
    keyword: str = Field(default="", max_length=32, description="产品名或类型，留空列全部")
    only_low_risk: bool = Field(default=False, description="只看稳健型")


def _query_products(
    conn: sqlite3.Connection, ctx: UserContext, args: QueryProductsArgs
) -> ToolResult:
    rows = find_wealth_products(conn, args.keyword) if args.keyword else []
    note = ""
    if not rows:
        # 查询类请求宁可多给信息，也不要只回一句「没有」。
        # 但必须**如实说明**没匹配上，不能让用户以为这是精确结果。
        rows = list(list_wealth_products(conn))
        if args.keyword:
            note = f"没有找到与「{args.keyword}」完全匹配的产品，以下是全部在售产品："
    if args.only_low_risk:
        rows = [r for r in rows if r["risk_level"] == "低"]
    items = [dict(r) for r in rows]
    return ToolResult(
        ok=True,
        tool="query_wealth_products",
        data={
            "items": items,
            "count": len(items),
            "note": note,
            "orders": [dict(r) for r in list_wealth_orders(conn, ctx.user_id)],
        },
    )


register(
    ToolMeta(
        name="query_wealth_products",
        label="查询理财产品",
        base_level=RiskLevel.GREEN,
        is_fund_movement=False,
        args_model=QueryProductsArgs,
        handler=_query_products,
        description="列出在售理财产品（类型/年化/起购金额/期限）及本人在持订单。",
    )
)


# ------------------------------------------------------------------ 申购

class PurchaseArgs(BaseModel):
    product: str = Field(min_length=1, max_length=32, description="产品名或类型，可模糊匹配")
    amount_cents: int = Field(gt=0, le=50_000_000, description="申购金额，单位分")


def _precheck_purchase(conn: sqlite3.Connection, user_id: str, args: PurchaseArgs) -> str | None:
    """在要人脸/短信之前，先把「买不成」的情况挡掉。"""
    product, err = _resolve_product(conn, args.product)
    if err:
        return err
    assert product is not None
    if args.amount_cents < product["min_amount_cents"]:
        return (
            f"「{product['name']}」的起购金额是 {product['min_amount_cents']} 分，"
            f"本次 {args.amount_cents} 分不足起购"
        )
    acc = get_account(conn, user_id, "current")
    if acc is None:
        return "活期账户异常，无法扣款"
    if args.amount_cents > acc["balance_cents"]:
        return f"活期余额不足（当前 {acc['balance_cents']} 分）"
    return None


def _purchase(conn: sqlite3.Connection, ctx: UserContext, args: PurchaseArgs) -> ToolResult:
    product, err = _resolve_product(conn, args.product)
    if err or product is None:
        return ToolResult(ok=False, tool="purchase_wealth", error=err or "产品解析失败")

    # 防御性复核：策略引擎判过一遍，这里只保证「不超过余额」这个物理事实
    acc = get_account(conn, ctx.user_id, "current")
    if acc is None:
        return ToolResult(ok=False, tool="purchase_wealth", error="活期账户异常")
    if args.amount_cents > acc["balance_cents"]:
        return ToolResult(ok=False, tool="purchase_wealth", error="余额不足")

    conn.execute(
        "UPDATE accounts SET balance_cents = balance_cents - ? WHERE id=?",
        (args.amount_cents, acc["id"]),
    )
    conn.execute(
        "INSERT INTO transactions(id,account_id,user_id,ts,direction,amount_cents,"
        "counterparty,category,memo) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            new_id("t_"), acc["id"], ctx.user_id, now_iso(), "out",
            args.amount_cents, product["name"], "理财", f"申购 {product['name']}",
        ),
    )
    order_id = insert_wealth_order(
        conn, user_id=ctx.user_id, product=product,
        amount_cents=args.amount_cents, trace_id=ctx.trace_id,
    )

    return ToolResult(
        ok=True,
        tool="purchase_wealth",
        data={
            "order_id": order_id,
            "product_name": product["name"],
            "product_kind": product["kind"],
            "annual_bp": product["annual_bp"],
            "risk_level": product["risk_level"],
            "lock_days": product["lock_days"],
            "amount_cents": args.amount_cents,
            "balance_after_cents": acc["balance_cents"] - args.amount_cents,
            "expected_annual_cents": (args.amount_cents * product["annual_bp"] + 5_000) // 10_000,
        },
    )


register(
    ToolMeta(
        name="purchase_wealth",
        label="理财产品申购",
        base_level=RiskLevel.RED,
        is_fund_movement=True,
        args_model=PurchaseArgs,
        handler=_purchase,
        description="用活期余额申购本行在售理财产品，成交后进入封闭期。",
        precheck=_precheck_purchase,
        # 理财申购要求做过风险测评（KYC 2 级）；转账只要求实名（1 级）
        min_kyc=2,
    )
)
