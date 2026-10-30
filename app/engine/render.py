"""★ 表述层 —— 幻觉防护的物理落点。

规定：**Agent 对用户说的每一句「已完成 / 余额是 / 金额是」，都必须由
工具返回的结构化结果渲染而来，模型无权自由转述交易结果。**

实现手段就是本模块：只有这里能把 ToolResult 变成人话，而它是纯字符串
模板，没有任何模型参与。因此「编造一笔不存在的转账」在架构上不可能发生。
"""

from __future__ import annotations

from app.schemas import Decision, DecisionOutcome, ToolResult


def cents_to_yuan(cents: int) -> str:
    """整数分 → 人民币字符串。全程整数运算，不碰浮点。"""
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    yuan, fen = divmod(cents, 100)
    return f"{sign}{yuan:,}.{fen:02d}"


def yuan(cents: int) -> str:
    return f"¥{cents_to_yuan(cents)}"


# ------------------------------------------------------------------ 单工具

def _render_query_balance(r: ToolResult) -> str:
    if not r.ok:
        return f"查询失败：{r.error}"
    kind = "活期账户" if r.data["account_kind"] == "current" else "储蓄账户"
    return f"您的{kind}余额为 {yuan(r.data['balance_cents'])}。"


def _render_query_transactions(r: ToolResult) -> str:
    if not r.ok:
        return f"查询失败：{r.error}"
    items = r.data["items"]
    days = r.data["days"]
    if not items:
        return f"近 {days} 天没有符合条件的交易记录。"
    total_out = sum(i["amount_cents"] for i in items if i["direction"] == "out")
    head = f"近 {days} 天共 {len(items)} 笔交易，支出合计 {yuan(total_out)}。"
    lines = [
        f"· {i['ts'][:10]} {i['counterparty'] or '—'} {yuan(i['amount_cents'])}"
        f"{'（' + i['memo'] + '）' if i.get('memo') else ''}"
        for i in items[:5]
    ]
    more = f"\n……另有 {len(items) - 5} 笔" if len(items) > 5 else ""
    return head + "\n" + "\n".join(lines) + more


def _pct(bp: int) -> str:
    """万分比 → 百分比字符串，全程整数运算。1234 → '12.3%'。"""
    return f"{bp // 100}.{bp % 100 // 10}%"


def _pct2(bp: int) -> str:
    """万分比 → 两位百分比。285 → '2.85%'。理财年化用这个，少一位就成了错数。"""
    return f"{bp // 100}.{bp % 100:02d}%"


def _render_analyze_spending(r: ToolResult) -> str:
    if not r.ok:
        return f"账单分析未完成：{r.error}"
    d = r.data
    goal, days = d["goal"], d["days"]
    res = d.get("result") or {}
    sbx = d.get("sandbox") or {}
    tail = (
        f"\n（分析代码由「{sbx.get('generator', '—')}」生成，在沙箱子进程内执行 "
        f"{sbx.get('elapsed_ms', 0)} ms，代码指纹 {sbx.get('code_digest', '—')}）"
    )

    if goal == "category_breakdown":
        rows = res.get("rows") or []
        if not rows:
            return f"近 {days} 天没有支出记录。"
        head = (f"近 {days} 天共 {res.get('count', 0)} 笔支出，合计 "
                f"{yuan(res.get('total_cents', 0))}。支出结构：")
        lines: list[str] = []
        for row in rows[:6]:
            lines.append(
                f"  {row['category']:<8}{yuan(row['cents']):>12}   {_pct(row['pct_bp'])}"
                f" · {row.get('count', 0)} 笔"
            )
            # ★ 占比大的类目必须能下钻：只给一个「其他 47.7%」等于没分析。
            tops = row.get("top") or []
            if tops and (row["category"] == "其他" or row["pct_bp"] >= 2000):
                lines.append(
                    "      └ " + "、".join(f"{t['merchant']} {yuan(t['cents'])}" for t in tops)
                )
        return head + "\n" + "\n".join(lines) + tail

    if goal == "top_merchants":
        rows = res.get("rows") or []
        if not rows:
            return f"近 {days} 天没有支出记录。"
        head = (f"近 {days} 天支出合计 {yuan(res.get('total_cents', 0))}，"
                f"花钱最多的商户：")
        lines = [
            f"  {i}. {row['merchant']:<10}{yuan(row['cents']):>12}   {_pct(row['pct_bp'])}"
            for i, row in enumerate(rows, 1)
        ]
        return head + "\n" + "\n".join(lines) + tail

    if goal == "anomalies":
        rows = res.get("rows") or []
        scanned = res.get("scanned", 0)
        if not rows:
            return f"近 {days} 天扫描 {scanned} 笔支出，未发现异常交易。"
        head = (f"近 {days} 天扫描 {scanned} 笔支出，发现 {res.get('count', 0)} 笔异常，"
                f"涉及 {yuan(res.get('flagged_cents', 0))}：")
        lines = [
            f"  {row['ts'][:16].replace('T', ' ')}  {row['counterparty'] or '—'}"
            f"  {yuan(row['cents'])}  ← {'、'.join(row['reasons'])}"
            for row in rows[:5]
        ]
        return head + "\n" + "\n".join(lines) + tail

    if goal == "trend":
        rows = res.get("rows") or []
        if not rows:
            return f"近 {days} 天没有支出记录。"
        head = (f"近 {days} 天支出合计 {yuan(res.get('total_cents', 0))}，"
                f"按周分布：")
        lines = [f"  {row['week']:<14}{yuan(row['cents'])}" for row in rows]
        return head + "\n" + "\n".join(lines) + tail

    return f"分析完成（{goal}），但表述层没有对应的模板。" + tail


def _render_transfer(r: ToolResult) -> str:
    if not r.ok:
        return f"转账未完成：{r.error}"
    d = r.data
    if d.get("scheduled"):
        return (
            f"已预约于 {d['schedule_date']} 向 {d['to_name']}"
            f"（{d['to_bank']} {d['to_account_masked']}）转账 {yuan(d['amount_cents'])}。"
        )
    memo = f"，备注「{d['memo']}」" if d.get("memo") else ""
    return (
        f"已向 {d['to_name']}（{d['to_bank']} {d['to_account_masked']}）"
        f"转账 {yuan(d['amount_cents'])}{memo}。\n"
        f"今日累计转账 {yuan(d['daily_total_after_cents'])}。"
    )


def _render_query_subscriptions(r: ToolResult) -> str:
    if not r.ok:
        return f"订阅查询失败：{r.error}"
    d = r.data
    items = d["items"]
    if not items:
        return "您当前没有任何生效中的订阅或代扣。"
    head = (
        f"您当前有 {d['count']} 项订阅，按当前价格折算："
        f"每月 {yuan(d['monthly_total_cents'])}，"
        f"全年约 {yuan(d['annualized_cents'])}。"
    )
    lines = [
        f"  · {i['merchant']}  {yuan(i['amount_cents'])}/{'月' if i['period'] == 'monthly' else '年'}"
        f"  下次扣款 {i['next_charge_date'] or '—'}"
        for i in items
    ]
    return head + "\n" + "\n".join(lines)


def _render_cancel_subscription(r: ToolResult) -> str:
    if not r.ok:
        return f"取消订阅未完成：{r.error}"
    d = r.data
    when = "已立即停止扣款" if d["cancel_mode"] == "immediate" else "本期到期后不再自动续费"
    return (
        f"已为您取消「{d['merchant']}」的订阅（原 {yuan(d['amount_cents'])}/"
        f"{'月' if d['period'] == 'monthly' else '年'}），{when}。\n"
        f"预计每年可省下 {yuan(d['saved_annual_cents'])}。"
    )


_STATUS_CN = {"active": "正常", "frozen": "已挂失", "locked": "交易锁定"}


def _render_query_cards(r: ToolResult) -> str:
    if not r.ok:
        return f"卡片查询失败：{r.error}"
    items = r.data["items"]
    if not items:
        return "您名下还没有任何银行卡。"
    head = f"您名下有 {r.data['count']} 张卡"
    head += f"，其中 {r.data['frozen_count']} 张处于非正常状态。" if r.data["frozen_count"] else "。"
    lines = []
    for i in items:
        limit = (
            f"信用额度 {yuan(i['credit_limit_cents'])}" if i["kind"] == "credit"
            else f"单日限额 {yuan(i['daily_limit_cents'])}"
        )
        extra = f"（{i['frozen_reason']}）" if i.get("frozen_reason") else ""
        lines.append(
            f"  · {i['kind_cn']} {i['brand']} 尾号{i['last4']}  {limit}  "
            f"{_STATUS_CN.get(i['status'], i['status'])}{extra}"
        )
    return head + "\n" + "\n".join(lines)


def _render_apply_virtual_card(r: ToolResult) -> str:
    if not r.ok:
        return f"虚拟卡申请未完成：{r.error}"
    d = r.data
    return (
        f"已为您开立一张虚拟卡：{d['brand']} 尾号{d['last4']}，"
        f"单日限额 {yuan(d['daily_limit_cents'])}，可用于小额网络支付。\n"
        f"卡号与安全码可在「我的卡片」中查看，此处不回显。"
    )


def _render_freeze_card(r: ToolResult) -> str:
    if not r.ok:
        return f"挂失未完成：{r.error}"
    d = r.data
    return (
        f"已挂失 {d['kind_cn']}·{d['brand']}（尾号{d['last4']}），该卡交易已立即阻断。\n"
        f"事由：{d['reason']}。如卡片找回，可发起「解挂」（需再次身份验证）。"
    )


def _render_unfreeze_card(r: ToolResult) -> str:
    if not r.ok:
        return f"解挂未完成：{r.error}"
    d = r.data
    return (
        f"已解除 {d['kind_cn']}·{d['brand']}（尾号{d['last4']}）的挂失，该卡恢复正常交易。"
    )


def _render_query_wealth_products(r: ToolResult) -> str:
    if not r.ok:
        return f"理财查询失败：{r.error}"
    items = r.data["items"]
    if not items:
        return "当前没有在售的理财产品。"
    lines = [
        f"  · {i['name']:<12}{i['kind']}  {_pct2(i['annual_bp'])}  "
        f"起购 {yuan(i['min_amount_cents'])}  "
        + ("随时赎回" if i["lock_days"] == 0 else f"封闭 {i['lock_days']} 天")
        + f"  风险{i['risk_level']}"
        for i in items
    ]
    prefix = r.data.get("note") or f"在售理财 {r.data['count']} 只"
    text = prefix + "（年化为业绩比较基准，不代表实际收益）：\n" + "\n".join(lines)
    orders = r.data.get("orders") or []
    if orders:
        held = sum(o["amount_cents"] for o in orders)
        text += f"\n您已持有 {len(orders)} 笔，合计 {yuan(held)}。"
    return text


def _render_purchase_wealth(r: ToolResult) -> str:
    if not r.ok:
        return f"申购未完成：{r.error}"
    d = r.data
    lock = "无封闭期，可随时赎回" if d["lock_days"] == 0 else f"封闭 {d['lock_days']} 天"
    return (
        f"已申购「{d['product_name']}」{yuan(d['amount_cents'])}"
        f"（{d['product_kind']}型，业绩比较基准 {_pct2(d['annual_bp'])}，{lock}，风险{d['risk_level']}）。\n"
        f"按基准测算年化收益约 {yuan(d['expected_annual_cents'])}；"
        f"活期余额剩余 {yuan(d['balance_after_cents'])}。\n"
        f"订单号 {d['order_id']}"
    )


_RENDERERS = {
    "query_balance": _render_query_balance,
    "query_transactions": _render_query_transactions,
    "analyze_spending": _render_analyze_spending,
    "query_subscriptions": _render_query_subscriptions,
    "cancel_subscription": _render_cancel_subscription,
    "transfer": _render_transfer,
    "query_cards": _render_query_cards,
    "apply_virtual_card": _render_apply_virtual_card,
    "freeze_card": _render_freeze_card,
    "unfreeze_card": _render_unfreeze_card,
    "query_wealth_products": _render_query_wealth_products,
    "purchase_wealth": _render_purchase_wealth,
}


def render_result(r: ToolResult) -> str:
    fn = _RENDERERS.get(r.tool)
    if fn is None:
        # 未注册渲染器的工具，只输出结构化事实，不让模型自由发挥
        return f"操作 {r.tool} 已{'完成' if r.ok else '失败'}。"
    return fn(r)


# ------------------------------------------------------------------ 待确认

def _facts_card(d: Decision, step) -> str:
    """确认卡片：把「对谁、对哪个账户、多少钱、转完还剩多少」全摊开。

    ★ 只写「向「妈」转账 ¥10,000」是不够的——「妈」是个别名，
      别名背后是哪个银行、哪个尾号，用户在最该看清的那一刻反而看不见。
      这张卡片就是社工与误转的最后一道人工闸门。
    """
    f = d.facts or {}
    head = _describe_step(step)
    if not f.get("recipient_name"):
        return f"  {head}"

    who = f"「{f['recipient_input']}」" if f.get("recipient_input") else ""
    bank = f.get("recipient_bank") or ""
    last4 = f.get("recipient_last4") or ""
    target = str(f["recipient_name"])
    detail = f"{bank}{' ' if bank and last4 else ''}{'****' + last4 if last4 else ''}".strip()
    if detail:
        target += f"（{detail}）"

    rows = [f"  向 {who}{target} 转账 {yuan(f.get('amount_cents', 0))}"]
    rows.append(f"  手续费 {yuan(f.get('fee_cents', 0))}，{f.get('eta', '实时到账')}")
    if "balance_before_cents" in f:
        after = f["balance_before_cents"] - f.get("amount_cents", 0)
        rows.append(f"  活期余额 {yuan(f['balance_before_cents'])} → 转后 {yuan(after)}")
    if "daily_limit_cents" in f:
        rows.append(
            f"  今日已转 {yuan(f.get('daily_used_cents', 0))}，"
            f"本笔后剩余额度 {yuan(f.get('daily_remaining_cents', 0))}"
            f"（限额 {yuan(f['daily_limit_cents'])}）"
        )
    if f.get("first_time"):
        rows.append("  ⚠️ 这是你第一次向该收款人转账，请核对姓名与尾号")
    return "\n".join(rows)


def render_pending(decisions: list[Decision], steps, masked_phone: str | None = None) -> str:
    """渲染需要用户确认 / 多因子认证的提示。同样不经过模型。

    ★ 注意这里**没有** mfa_code 参数：动态码走短信网关（`app/bank/sms.py`），
      不进聊天回复。能改写对话的攻击者，也就能读到对话里的验证码——
      把两者放在同一条通道，「双因子」就只剩一个因子。
    """
    lines: list[str] = []
    step_map = {s.id: s for s in steps}

    for d in decisions:
        step = step_map.get(d.step_id)
        if step is None:
            continue
        card = _facts_card(d, step)
        if d.outcome == DecisionOutcome.CONFIRM:
            lines.append(
                f"⚠️ 以下操作需要您确认：\n{card}\n  （{d.reason}）\n  回复「确认」后执行。"
            )
        elif d.outcome == DecisionOutcome.MFA:
            hint = (
                f"\n  📱 动态码已发送到绑定手机 {masked_phone}。"
                f"\n     请打开右上角「📱 手机」查看——它不在本对话里，"
                f"这正是双因子的意义：能改写对话的人拿不到它。"
                if masked_phone
                else ""
            )
            lines.append(
                f"🔐 以下操作属于大额/高风险，需要多重身份验证：\n{card}\n  （{d.reason}）{hint}"
            )
    return "\n\n".join(lines)


def describe_step(step) -> str:
    """公开版：编排层要拿「这一步本来要干什么」来渲染跳过提示。

    刻意复用同一个函数，而不是另写一份描述——两份描述迟早会说不一样的话，
    而用户看到的跳过提示必须和他刚才确认的是同一件事。
    """
    return _describe_step(step)


def _describe_step(step) -> str:
    args = step.args
    if step.tool == "transfer":
        amount = args.get("amount_cents", 0)
        return f"向「{args.get('to')}」转账 {yuan(amount)}"
    if step.tool == "cancel_subscription":
        when = "立即停止" if args.get("period") == "immediate" else "到期不再续费"
        return f"取消「{args.get('merchant')}」的订阅（{when}）"
    if step.tool == "apply_virtual_card":
        # 确认提示里必须显示**将要生效的真实限额**，包括默认值。
        # 早期这里写的是 args.get(...) 或 0，结果提示「单日限额 ¥0.00」——
        # 用户会以为申请的是张废卡，而实际执行的是 1000 元额度。
        from app.tools.card import DEFAULT_VIRTUAL_DAILY_CENTS

        limit = args.get("daily_limit_cents")
        if limit is None:
            limit = DEFAULT_VIRTUAL_DAILY_CENTS
        return f"申请一张虚拟卡（单日限额 {yuan(limit)}）"
    if step.tool == "freeze_card":
        return f"挂失卡片「{args.get('card')}」（该卡将被立即阻断交易）"
    if step.tool == "unfreeze_card":
        return f"解除卡片「{args.get('card')}」的挂失（该卡将恢复交易能力）"
    if step.tool == "purchase_wealth":
        return f"申购「{args.get('product')}」{yuan(args.get('amount_cents', 0))}"
    return f"{step.tool} {args}"
