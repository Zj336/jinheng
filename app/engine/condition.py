"""条件步骤求值 —— 纯比较，没有 eval。

一条 when 条件只做三件事：
  1. 找到被引用步骤的**真实执行结果**（不是模型说的，是工具返回的）
  2. 从结果里取一个字段
  3. 跟一个字面量做一次比较

任何一步取不到（步骤没执行、字段不存在、类型不可比），一律返回「不成立」。
理由和整个项目一致：**fail-closed**。拿不准就不要执行——
对一个「余额够就转」的计划来说，跳过比误转安全一万倍。
"""

from __future__ import annotations

from typing import Any

from app.schemas import StepCondition


def _compare(cur: Any, op: str, expected: Any) -> bool:
    try:
        if op == ">":
            return bool(cur > expected)
        if op == ">=":
            return bool(cur >= expected)
        if op == "<":
            return bool(cur < expected)
        if op == "<=":
            return bool(cur <= expected)
        if op == "==":
            return bool(cur == expected)
        if op == "!=":
            return bool(cur != expected)
    except TypeError:
        # 字符串和整数比较这类，Python 会抛 TypeError。
        # 不猜、不转换，直接当条件不成立。
        return False
    return False


def evaluate(cond: StepCondition, results: dict[str, dict]) -> tuple[bool, str]:
    """返回 (条件是否成立, 人话解释)。解释会原样进审计日志和用户可见文本。"""
    data = results.get(cond.step)
    if data is None:
        return False, f"前置步骤 {cond.step} 没有执行，无法确认条件"

    cur: Any = data
    for part in cond.field.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, f"前置结果里没有字段「{cond.field}」"
        cur = cur[part]

    ok = _compare(cur, cond.op, cond.value)
    verdict = "成立" if ok else "不成立"
    return ok, f"{cond.step}.{cond.field} = {cur!r}，条件 {cond.op} {cond.value!r} {verdict}"
