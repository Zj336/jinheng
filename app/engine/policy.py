"""★ 确定性权限策略引擎 —— 本项目的技术地基。

════════════════════════════════════════════════════════════════
核心断言：权限判定不能依赖 LLM。
════════════════════════════════════════════════════════════════
把权限规则写进 prompt 的系统，一定会被提示注入绕过——因为注入的目标
恰恰就是「让模型相信规则已经改变」。所以本引擎刻意做到：

  1. 它不接收任何自然语言输入，只接收结构化 Step（工具名 + 参数）和
     UserContext（全部由数据库确定性拼装）。
  2. 它不认识「我是管理员」「风控已批准」「ignore previous instructions」
     这类声明——这些字符串在类型层面就无法进入本模块。
  3. 风险等级只升不降（见 tools.base.escalate），不存在降低等级的代码路径。

因此即便 LLM 被完全越狱，它最多能产出一个**不合法的计划**，
而这个计划会在两层被拦下：DAG 校验器（结构层）+ 本引擎（语义层）。
"""

from __future__ import annotations

import sqlite3

from pydantic import ValidationError

from app.bank.db import (
    get_account,
    get_contact,
    has_paid_contact,
    monthly_transferred_cents,
    transferred_to_contact_today_cents,
)
from app.config import settings
from app.schemas import Decision, DecisionOutcome, Plan, RiskLevel, Step, UserContext
from app.tools.base import LEVEL_TO_OUTCOME, escalate, get_tool
from app.tools.transfer import resolve_contact

# ------------------------------------------------------------------ 规则表
# 每一条都会原样出现在审计日志里，也直接构成《安全自评报告》的规则清单。
POLICY_RULES: dict[str, str] = {
    "P-000": "会话已被熔断锁定，一切操作拒绝",
    "P-001": "工具不在白名单，拒绝执行",
    "P-010": "参数未通过 Schema 校验（类型/范围/长度）",
    "P-011": "操作目标无法唯一解析（没找到或有歧义）",
    "P-020": "目标账户或收款人不属于当前用户（越权）",
    "P-030": "按工具声明的风险基级判定",
    # ★ 限额是**硬约束**，不是风险信号。强验证解决「你是本人吗」，
    #   解决不了「这笔钱本来就不许转」。2026-10-30 之前 P-040 只升到红色，
    #   于是超过日限额的转账只要给验证码就能放行——限额形同虚设。
    "P-040": "当日累计转账超出限额，硬性拒绝（强验证不可覆盖）",
    "P-041": "首次向该收款人转账，风险升级一档",
    "P-042": "当月累计转账超出限额，硬性拒绝",
    "P-043": "同一收款人当日累计超出限额，硬性拒绝",
    "P-044": "单笔金额达到强验证阈值，升级为红色",
    "P-050": "账户 KYC 等级不足，禁止该操作",
    "P-070": "风险适当性不匹配：产品风险等级高于客户风险偏好，硬性拒绝",
    "P-090": "一句话里命中提示注入特征，且请求涉及资金或高风险操作，整轮阻断",
    "P-061": "本会话已完成用户确认，黄色放行",
    "P-062": "本会话已完成多因子认证，红色放行",
}


def _yuan(cents: int) -> str:
    return f"¥{cents / 100:,.2f}"


class PolicyEngine:
    """无状态、纯函数式。给同样的 (plan, ctx, db) 必然得到同样的裁决。"""

    def __init__(
        self,
        daily_limit_cents: int | None = None,
        monthly_limit_cents: int | None = None,
        contact_daily_limit_cents: int | None = None,
        single_confirm_limit_cents: int | None = None,
    ) -> None:
        self.daily_limit_cents = (
            daily_limit_cents if daily_limit_cents is not None else settings.daily_transfer_limit_cents
        )
        self.monthly_limit_cents = (
            monthly_limit_cents if monthly_limit_cents is not None else settings.monthly_transfer_limit_cents
        )
        self.contact_daily_limit_cents = (
            contact_daily_limit_cents
            if contact_daily_limit_cents is not None
            else settings.per_contact_daily_limit_cents
        )
        self.single_confirm_limit_cents = (
            single_confirm_limit_cents
            if single_confirm_limit_cents is not None
            else settings.single_confirm_limit_cents
        )

    # -------------------------------------------------------------- 计划级

    def adjudicate_plan(
        self, conn: sqlite3.Connection, plan: Plan, ctx: UserContext
    ) -> list[Decision]:
        """按拓扑序逐个裁决。

        ★ 同一计划内的多笔转账必须**累计**计算，不能各判各的。
          否则用户在一个计划里连说两次「转 600」就能绕过 1000 的日限额。
        """
        decisions: list[Decision] = []
        running_daily = ctx.daily_transferred_cents

        for step in plan.steps:
            decision, amount = self.adjudicate_step(conn, step, ctx, running_daily)
            decisions.append(decision)
            # 未被直接拒绝的步骤，按「将会执行」计入累计（保守估计）
            if amount is not None and decision.outcome != DecisionOutcome.DENY:
                running_daily += amount
        return decisions

    # -------------------------------------------------------------- 步骤级

    def adjudicate_step(
        self,
        conn: sqlite3.Connection,
        step: Step,
        ctx: UserContext,
        running_daily: int | None = None,
    ) -> tuple[Decision, int | None]:
        """返回 (裁决, 该步骤涉及的金额（分）或 None)。"""
        running_daily = ctx.daily_transferred_cents if running_daily is None else running_daily

        def deny(level: RiskLevel, reason: str, hit: str) -> tuple[Decision, None]:
            return (
                Decision(
                    step_id=step.id,
                    tool=step.tool,
                    level=level,
                    outcome=DecisionOutcome.DENY,
                    reason=reason,
                    policy_hits=[hit],
                ),
                None,
            )

        # ---- 0. 熔断锁定 ----
        if ctx.locked:
            return deny(RiskLevel.RED, "会话已被安全锁定，请重新登录后操作", "P-000")

        # ---- 1. 工具白名单 ----
        meta = get_tool(step.tool)
        if meta is None:
            return deny(RiskLevel.RED, f"未知或不支持的操作：{step.tool}", "P-001")

        # ---- 2. 参数 Schema 校验 ----
        try:
            args = meta.args_model.model_validate(step.args)
        except ValidationError as exc:
            brief = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:3])
            return deny(RiskLevel.RED, f"参数不合法（{brief}）", "P-010")

        # ---- 3. 前置解析：目标找不找得到、唯不唯一 ----
        # 必须发生在「让用户确认 / 做多因子」之前 —— 否则用户认证完才发现
        # 操作对象根本不存在，是纯粹的体验与信任损耗。
        if meta.precheck is not None:
            problem = meta.precheck(conn, ctx.user_id, args)
            if problem:
                # 前置检查可以自己说明该记哪条规则：解析不到目标 = P-011（问一句就能解决），
                # 风险适当性不匹配 = P-070（合规硬拦，说多少遍都解决不了）。
                if isinstance(problem, tuple):
                    reason, hit = problem
                    # 除「目标解析」外的前置拒绝都是硬性问题，按红色记
                    level = RiskLevel.YELLOW if hit == "P-011" else RiskLevel.RED
                else:
                    reason, hit, level = problem, "P-011", RiskLevel.YELLOW
                return deny(level, reason, hit)

        # ---- 4. 非资金类：直接按基级 ----
        if not meta.is_fund_movement:
            if ctx.kyc_level < meta.min_kyc:
                return deny(RiskLevel.RED, f"账户等级不足（需 KYC {meta.min_kyc} 级），禁止该操作", "P-050")
            return self._finalize(step, meta, meta.base_level, ctx, ["P-030"], ""), None

        # ---- 5. 资金类：越权检查 ----
        # ★ 只有声明了 resolves_contact 的工具才在这里解析收款人。
        #   引擎不再认识「转账」这个具体工具，因此新增理财申购之类的资金类工具
        #   不需要改本文件一行（旧写法是 assert isinstance(args, TransferArgs)，
        #   第二个资金类工具一注册就会断言失败）。
        amount = int(getattr(args, "amount_cents", 0) or 0)
        if amount <= 0 and not meta.amount_optional:
            # 声明了动钱却拿不到金额，属于工具定义错误。fail-closed。
            # ★ 例外只有一处：工具自己声明 amount_optional=True（理财赎回的
            #   「全部赎回」），此时 0/None 有确定语义，不是信息缺失。
            #   缺金额仍是默认拒绝——这条不能因为个别工具放宽而整体放宽。
            return deny(RiskLevel.RED, "资金类操作缺少有效金额，已拒绝", "P-010")

        hits = ["P-030"]
        level = meta.base_level
        facts: dict = {"amount_cents": amount}

        if meta.resolves_contact:
            contact, candidates = resolve_contact(conn, ctx.user_id, args.to)  # type: ignore[attr-defined]
            if contact is None:
                reason = (
                    f"收款人「{args.to}」有歧义，请在 {[c['name'] for c in candidates]} 中明确"  # type: ignore[attr-defined]
                    if len(candidates) > 1
                    else f"未找到收款人「{args.to}」"  # type: ignore[attr-defined]
                )
                return deny(RiskLevel.YELLOW, reason, "P-011")

            # 二次确认归属：即使 resolve_contact 已经限定在本用户范围，这里再查一遍，
            # 防止将来有人改动 resolve_contact 时把越权防线拆掉。
            if get_contact(conn, ctx.user_id, contact["id"]) is None:
                return deny(RiskLevel.RED, "该收款人不属于当前用户，已拒绝", "P-020")

            # ---- 6. ★ 限额硬校验（四道）。超限 = 拒绝，不是升级。----
            #
            # 旧实现的写法是「超限就把等级升到 RED」，而 RED 可以被多因子认证放行。
            # 结果 2026-10-30 实跑时：300 + 300 + 500 三笔把 1000 元的日限额顶穿，
            # 第四笔 10000 元只要给验证码照样执行。**限额不是限额，是个提示。**
            # 强验证解决的是「你是本人吗」，它无权改变「这一笔本来就不许转」。
            projected = running_daily + amount
            if projected > self.daily_limit_cents:
                return deny(
                    RiskLevel.RED,
                    f"今日已转 {_yuan(running_daily)}，这笔 {_yuan(amount)} 将累计到 "
                    f"{_yuan(projected)}，超过单日限额 {_yuan(self.daily_limit_cents)}（P-040）。\n"
                    f"   今日剩余可转 {_yuan(max(0, self.daily_limit_cents - running_daily))}。"
                    f"限额是硬约束，人脸/短信验证**不能**突破它；"
                    f"如需当日提额，回复「转人工」走人工复核。",
                    "P-040",
                )
            month_used = monthly_transferred_cents(conn, ctx.user_id)
            if month_used + amount > self.monthly_limit_cents:
                return deny(
                    RiskLevel.RED,
                    f"本月已转 {_yuan(month_used)}，这笔 {_yuan(amount)} 将超过单月限额 "
                    f"{_yuan(self.monthly_limit_cents)}（P-042）。",
                    "P-042",
                )
            contact_used = transferred_to_contact_today_cents(conn, ctx.user_id, contact["id"])
            if contact_used + amount > self.contact_daily_limit_cents:
                return deny(
                    RiskLevel.RED,
                    f"今天已向「{contact['name']}」转 {_yuan(contact_used)}，这笔 {_yuan(amount)} "
                    f"将超过单一收款人日限额 {_yuan(self.contact_daily_limit_cents)}（P-043）。"
                    f"分散到多个收款人同样受限，这条规则就是防这件事的。",
                    "P-043",
                )

            # ---- 7. 单笔阈值 / 首次收款人 → 升级 ----
            first_time = not has_paid_contact(conn, ctx.user_id, contact["id"])
            if amount >= self.single_confirm_limit_cents:
                level = escalate(escalate(level, 1), 1) if first_time else escalate(level, 1)
                hits.append("P-044")
            elif first_time:
                level = escalate(level, 1)
            if first_time:
                hits.append("P-041")

            account_no = str(contact["account_no"] or "")
            try:
                acct = get_account(conn, ctx.user_id, "current")
                if acct is not None:
                    # 确认卡片要能回答「转完还剩多少」——这是用户真正关心的问题
                    facts["balance_before_cents"] = int(acct["balance_cents"])
            except Exception:  # noqa: BLE001  账户读不到就不显示余额，不影响裁决
                pass
            facts.update(
                {
                    "recipient_input": getattr(args, "to", ""),
                    "recipient_name": contact["name"],
                    "recipient_bank": contact["bank"] or "",
                    "recipient_last4": account_no[-4:] if len(account_no) >= 4 else "",
                    "contact_id": contact["id"],
                    "fee_cents": 0,
                    "eta": "实时到账",
                    "first_time": first_time,
                    "daily_used_cents": running_daily,
                    "daily_limit_cents": self.daily_limit_cents,
                    "daily_remaining_cents": max(0, self.daily_limit_cents - projected),
                }
            )
        else:
            # 非收款人型资金操作（如理财申购）：不占转账日额度，
            # 靠「基级即红色 + 每次都要强验证」来兜底，不存在拆单绕过的空间。
            projected = running_daily

        # ---- 8. KYC ----
        if ctx.kyc_level < meta.min_kyc:
            return deny(RiskLevel.RED, f"账户等级不足（需 KYC {meta.min_kyc} 级），禁止该操作", "P-050")

        if meta.resolves_contact:
            reason = (
                f"本次 {_yuan(amount)}；今日累计将为 {_yuan(projected)} / 限额 "
                f"{_yuan(self.daily_limit_cents)}；风险等级 {level.value}"
                + ("；该收款人是首次收款" if facts.get("first_time") else "")
            )
        else:
            # amount_optional 的工具（理财赎回）允许「不填金额 = 全部」，
            # 此时回显不能说「本次 ¥0.00」——那是把「全部」说成了「零」。
            what = _yuan(amount) if amount > 0 else "全部持仓"
            reason = f"本次 {what}；风险等级 {level.value}"
        # 只有真正写入转账台账的操作才计入当日累计，
        # 否则「买完理财就转不出钱」——那不是安全，是 bug。
        return self._finalize(step, meta, level, ctx, hits, reason, facts), (
            amount if meta.resolves_contact else None
        )

    # -------------------------------------------------------------- 内部

    def _finalize(
        self,
        step: Step,
        meta,
        level: RiskLevel,
        ctx: UserContext,
        hits: list[str],
        reason: str,
        facts: dict | None = None,
    ) -> Decision:
        """把风险等级映射成执行策略，并处理「本会话已验证」的放行。"""
        outcome = LEVEL_TO_OUTCOME[level]

        # ★ 一次性放行凭证：只放行**用户实际确认过的那一步**，且用过即失效。
        #   修之前只看「本会话有 confirm/mfa 凭证」，不看是哪一步——
        #   于是一句「确认」会连带放行计划里同级别的后续步骤（卡片上甚至没列出来）。
        #   ctx.verified_for_step 由 orchestrator 在恢复执行前写入被确认的 step_id。
        same_step = bool(ctx.verified_for_step) and ctx.verified_for_step == step.id
        if outcome == DecisionOutcome.CONFIRM and "confirm" in ctx.verified_factors and same_step:
            outcome = DecisionOutcome.ALLOW
            hits = hits + ["P-061"]
        elif outcome == DecisionOutcome.MFA and "mfa" in ctx.verified_factors and same_step:
            outcome = DecisionOutcome.ALLOW
            hits = hits + ["P-062"]

        required: list[str] = []
        if outcome == DecisionOutcome.CONFIRM:
            required = ["user_confirm"]
        elif outcome == DecisionOutcome.MFA:
            required = ["face", "sms", "u_shield"]

        return Decision(
            step_id=step.id,
            tool=meta.name,
            level=level,
            outcome=outcome,
            reason=reason or f"{meta.label}，风险等级 {level.value}",
            policy_hits=hits,
            required=required,
            facts=facts or {},
        )
