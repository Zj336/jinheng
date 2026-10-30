"""异常熔断。

触发条件（任一满足即锁定会话）：
  * 连续被拒 / 校验失败次数 ≥ circuit_fail_threshold
  * 注入特征累计命中 ≥ circuit_inject_threshold

锁定后策略引擎的第一条规则 P-000 会让一切操作直接 DENY，
必须重新登录（新 session）才能解除。
"""

from __future__ import annotations

from app.config import settings
from app.schemas import UserContext


def record_failure(ctx: UserContext) -> None:
    ctx.denied_count += 1
    _maybe_lock(ctx)


def record_injections(ctx: UserContext, hits: list[str]) -> None:
    if not hits:
        return
    ctx.injection_hits += len(hits)
    _maybe_lock(ctx)


def reset_failures(ctx: UserContext) -> None:
    """一次成功执行后清零「连续失败」计数，但不清注入计数。"""
    ctx.denied_count = 0


def _maybe_lock(ctx: UserContext) -> None:
    if ctx.locked:
        return
    if ctx.denied_count >= settings.circuit_fail_threshold:
        ctx.locked = True
    if ctx.injection_hits >= settings.circuit_inject_threshold:
        ctx.locked = True


def status(ctx: UserContext) -> dict:
    return {
        "locked": ctx.locked,
        "denied_count": ctx.denied_count,
        "injection_hits": ctx.injection_hits,
        "fail_threshold": settings.circuit_fail_threshold,
        "inject_threshold": settings.circuit_inject_threshold,
    }
