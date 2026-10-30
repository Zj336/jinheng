"""账单分析工具 —— 自然语言 → 生成分析代码 → 沙箱执行 → 结构化结论。

★ 为什么它可以是「绿色：自动执行」：
   因为它只读，而且执行被关在沙箱里。风险控制不靠「这次操作看起来无害」，
   靠的是「就算它干坏事，也只能读到这一份数据副本、只能活 5 秒」。

★ 工具只接收结构化参数（goal / days），不接受自然语言。自然语言在
   编排层就被解析成 Intent 了，到不了这里 —— 与转账工具同一套规矩。
"""

from __future__ import annotations

import sqlite3
from typing import Literal

from pydantic import BaseModel, Field

from app.bank.db import recent_transactions
from app.sandbox import codegen
from app.sandbox.runner import run_code
from app.schemas import RiskLevel, ToolResult, UserContext
from app.tools.base import ToolMeta, register

Goal = Literal["category_breakdown", "top_merchants", "anomalies", "trend"]


class AnalyzeArgs(BaseModel):
    goal: Goal = Field(default="category_breakdown", description="分析目标")
    days: int = Field(default=30, ge=1, le=90, description="回溯天数")


def _analyze(conn: sqlite3.Connection, ctx: UserContext, args: AnalyzeArgs) -> ToolResult:
    rows = recent_transactions(conn, ctx.user_id, args.days)
    items = [dict(r) for r in rows]

    payload = {"days": args.days, "items": items}
    code, generator = codegen.generate(args.goal, args.days)
    outcome = run_code(code, payload)

    sandbox_meta = {
        "generator": generator,
        "code_digest": outcome.code_digest,
        "elapsed_ms": outcome.elapsed_ms,
        "killed": outcome.killed,
        "code": codegen.preview(code),
    }

    # ---- ★ 契约校验：模型可以自由写代码，但产物必须对得上表述层 ----
    # 真机联调踩到的坑：模型写的代码算得对，但把结果组织成 {"breakdown": [...]}，
    # 而表述层读的是 {"rows": [...]} → 打印「近 30 天没有支出记录」，实际有 11 笔。
    # 没有异常、没有日志，只是安静地答错。所以加一道结构校验 + 模板回落。
    problem = None if outcome.ok else (outcome.error or "沙箱执行失败")
    if outcome.ok:
        problem = codegen.validate_result(args.goal, outcome.result, items)

    if problem:
        fb_code = codegen.template_code(args.goal)
        fb = run_code(fb_code, payload)
        fb_ok = fb.ok and codegen.validate_result(args.goal, fb.result, items) is None
        sandbox_meta["fallback"] = {"from": generator, "to": "template", "reason": problem}
        if not fb_ok:
            return ToolResult(
                ok=False,
                tool="analyze_spending",
                error=f"{problem}；确定性模板也未能产出可用结果：{fb.error or '结构不符'}",
                data={"goal": args.goal, "days": args.days, "sandbox": sandbox_meta},
            )
        # 如实记录：这份结论**不是**模型写的代码算出来的
        code, generator, outcome = fb_code, "template(fallback)", fb
        sandbox_meta.update({
            "generator": generator,
            "code_digest": outcome.code_digest,
            "elapsed_ms": outcome.elapsed_ms,
            "code": codegen.preview(code),
        })

    if not outcome.ok:
        return ToolResult(
            ok=False,
            tool="analyze_spending",
            error=outcome.error,
            data={"goal": args.goal, "days": args.days, "sandbox": sandbox_meta},
        )

    return ToolResult(
        ok=True,
        tool="analyze_spending",
        data={
            "goal": args.goal,
            "days": args.days,
            "result": outcome.result,
            "sandbox": sandbox_meta,
        },
    )


register(
    ToolMeta(
        name="analyze_spending",
        label="账单分析",
        base_level=RiskLevel.GREEN,
        is_fund_movement=False,
        args_model=AnalyzeArgs,
        handler=_analyze,
        description="用自然语言提出分析需求，由沙箱内生成并执行的代码对本人流水做统计。",
    )
)
