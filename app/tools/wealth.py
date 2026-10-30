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

from pydantic import BaseModel, Field, field_validator

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


# 风险偏好 → 允许购买的最高产品风险等级。
# ★ 不匹配就**硬拒**，而不是「升级到红色、验个码就放行」——
#   适当性管理约束的是「这笔产品该不该卖给你」，不是「你是不是本人」。
#   让多因子认证去覆盖它，和让验证码覆盖限额是同一个错误。
_RISK_ORDER = ["低", "中", "高"]
_MAX_RISK = {"保守": "低", "稳健": "低", "平衡": "中", "进取": "高"}


def _precheck_purchase(
    conn: sqlite3.Connection, user_id: str, args: PurchaseArgs
) -> str | tuple[str, str] | None:
    """在要人脸/短信之前，先把「买不成」的情况挡掉。"""
    product, err = _resolve_product(conn, args.product)
    if err:
        return err
    assert product is not None

    # ---- 适当性：产品风险 ≤ 客户偏好 ----
    user = conn.execute("SELECT risk_pref FROM users WHERE id=?", (user_id,)).fetchone()
    pref = (user["risk_pref"] if user else "") or ""
    allowed_max = _MAX_RISK.get(pref)
    if allowed_max is None:
        # 偏好缺失/取值不认识 → fail-closed：不知道怎么判就不卖
        return ("未查到您的风险测评结果，暂时无法购买理财（请先完成风险测评）", "P-070")
    if _RISK_ORDER.index(product["risk_level"]) > _RISK_ORDER.index(allowed_max):
        return (
            f"「{product['name']}」的风险等级是「{product['risk_level']}」，"
            f"高于您的风险偏好「{pref}」（最高可买「{allowed_max}」）。\n"
            f"   适当性管理不允许向您销售超出偏好的产品；验码、确认都不能覆盖这一条。\n"
            f"   如确需购买，请先到柜台/App 完成风险测评（本演示不提供在线改偏好的入口——"
            f"偏好只能由测评写入，不能由对话改写）。",
            "P-070",
        )

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


# ------------------------------------------------------------------ 赎回
#
# ★ 分级：赎回是**黄色**（用户确认），不是红色——这是全项目唯一一处
#   我们主动「不照抄赛题」的地方，理由写在 docs/权限分级规格.md §2.3：
#   申购把钱锁进封闭期，风险方向是「拿不回来」；赎回是客户取回自有资金，
#   方向相反。权责不对称的资金动作，分级也该不对称。
#   （赛题原文只把「理财产品申购」列为红色，未提赎回。）

class RedeemArgs(BaseModel):
    product: str = Field(default="", max_length=32, description="产品名；留空表示赎回全部持仓")
    amount_cents: int | None = Field(
        default=None, gt=0, le=100_000_000, description="赎回金额（分）；留空表示该产品全部赎回"
    )

    @field_validator("amount_cents", mode="before")
    @classmethod
    def _normalize_amount(cls, v):
        """模型会把「全部」原样塞进金额槽。

        ★ 真机联调实测：用户说「赎回全部稳健增利」，模型填了
          {"amount_cents": "全部"}，被 Schema 校验拒成 P-010「参数不合法」——
          用户看到的是系统坏了，其实只是「全部」不是整数。
          「全部赎回」在这个工具里本来就有合法表达（None），
          所以这里把「全部/清仓/空串」归一成 None；其余把「1000元」这类
          带单位的字符串归一成分，归一不了的原样交给校验报错，不编数。
        """
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            if s in ("", "全部", "全赎回", "全赎", "不留", "清仓", "all", "ALL"):
                return None
            had_unit = any(u in s for u in ("元", "块"))
            s2 = s.replace(",", "").replace("，", "")
            for suffix in ("元", "块"):
                s2 = s2.replace(suffix, "")
            try:
                num = float(s2)
            except ValueError:
                return v
            if had_unit or "." in s2:
                return int(round(num * 100))
            return int(num)
        return v


def _precheck_redeem(conn: sqlite3.Connection, user_id: str, args: RedeemArgs) -> str | None:
    """在用户点确认之前，先把「赎不回来」的情况说清楚。"""
    from datetime import date, timedelta

    orders = [o for o in list_wealth_orders(conn, user_id) if o["status"] == "executed"]
    if args.product:
        orders = [o for o in orders if args.product in o["product_name"]]
    if not orders:
        return "没有可赎回的持仓" + (f"（产品「{args.product}」）" if args.product else "")

    today = date.today()
    locked = []
    for o in orders:
        p = get_wealth_product(conn, o["product_id"])
        lock_days = int(p["lock_days"] or 0) if p else 0
        if lock_days and (today - date.fromisoformat(o["ts"][:10])).days < lock_days:
            locked.append(o["product_name"])
    if locked and len(locked) == len(orders):
        return f"「{'、'.join(locked)}」仍在封闭期内，暂不可赎回"
    return None


def _redeem(conn: sqlite3.Connection, ctx: UserContext, args: RedeemArgs) -> ToolResult:
    from datetime import date

    orders = [o for o in list_wealth_orders(conn, ctx.user_id) if o["status"] == "executed"]
    if args.product:
        orders = [o for o in orders if args.product in o["product_name"]]
    if not orders:
        return ToolResult(ok=False, tool="redeem_wealth", error="没有可赎回的持仓")

    acc = get_account(conn, ctx.user_id, "current")
    if acc is None:
        return ToolResult(ok=False, tool="redeem_wealth", error="活期账户异常")

    budget = args.amount_cents  # None = 全部
    redeemed: list[dict] = []
    skipped: list[str] = []
    for o in orders:
        p = get_wealth_product(conn, o["product_id"])
        lock_days = int(p["lock_days"] or 0) if p else 0
        if lock_days and (date.today() - date.fromisoformat(o["ts"][:10])).days < lock_days:
            skipped.append(o["product_name"])
            continue
        take = int(o["amount_cents"]) if budget is None else min(budget, int(o["amount_cents"]))
        if take <= 0:
            break
        budget = None if budget is None else budget - take

        if take == int(o["amount_cents"]):
            conn.execute("UPDATE wealth_orders SET status='redeemed' WHERE id=?", (o["id"],))
        else:
            conn.execute(
                "UPDATE wealth_orders SET amount_cents=amount_cents-? WHERE id=?",
                (take, o["id"]),
            )
        conn.execute(
            "INSERT INTO transactions(id,account_id,user_id,ts,direction,amount_cents,"
            "counterparty,category,memo) VALUES (?,?,?,?,?,?,?,?,?)",
            (new_id("t_"), acc["id"], ctx.user_id, now_iso(), "in", take,
             o["product_name"], "理财", f"赎回 {o['product_name']}"),
        )
        redeemed.append({"product_name": o["product_name"], "amount_cents": take})

    total = sum(x["amount_cents"] for x in redeemed)
    if total <= 0:
        return ToolResult(
            ok=False, tool="redeem_wealth",
            error=f"「{'、'.join(skipped)}」仍在封闭期内，暂不可赎回",
        )

    conn.execute(
        "UPDATE accounts SET balance_cents = balance_cents + ? WHERE id=?", (total, acc["id"])
    )
    return ToolResult(
        ok=True,
        tool="redeem_wealth",
        data={
            "redeemed": redeemed,
            "total_cents": total,
            "balance_after_cents": int(acc["balance_cents"]) + total,
            "skipped": skipped,
        },
    )


register(
    ToolMeta(
        name="redeem_wealth",
        label="理财赎回",
        base_level=RiskLevel.YELLOW,
        # ★ 明确声明它不是「向联系人转账」类操作：不占转账日额度（否则赎回反而让人转不出钱）
        is_fund_movement=True,
        args_model=RedeemArgs,
        handler=_redeem,
        description="赎回本人持仓的理财产品（封闭期内不可赎回），资金回到活期账户。",
        precheck=_precheck_redeem,
        min_kyc=1,
        amount_optional=True,
    )
)
