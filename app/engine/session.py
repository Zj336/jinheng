"""会话态。

demo 阶段放内存即可；上生产换成 Redis。
这里只存「会话级风控上下文」，不存任何可被用户输入直接改写的授权信息。
"""

from __future__ import annotations

import threading

from app.bank.db import new_id
from app.schemas import UserContext

class SessionOwnershipError(Exception):
    """`session_id` 存在，但不属于当前登录用户。

    ★ 为什么必须有这一条：`session_id` 在响应体、前端状态、审计导出里都是明文，
      它不是令牌。此前 `get_or_create` 命中已有会话就直接返回，从不比对归属——
      于是任何人拿到别人的 `session_id`，后续裁决、执行、审计全部按**别人**的身份跑
      （读别人的卡、用别人挂着的验证码、从别人账户扣钱）。
      身份层的第三层是「数据归属」，这一条就是它的入口守卫。
    """

    def __init__(self, session_id: str) -> None:
        super().__init__(session_id)
        self.session_id = session_id


_lock = threading.Lock()
_sessions: dict[str, UserContext] = {}
# ★ 槽位续接：上一轮追问「还差什么」，这一轮用户很可能只补那一个词。
#   不记下来的话，用户答「2233」会被当成一句独立的话重新解析，得到「接不住」——
#   系统刚问完，转头就听不懂自己问的问题。
#   这份状态**只活一轮**：`take` 取走即清空；用户改说别的事时自然作废，不需要 TTL。
_clarify: dict[str, list[dict]] = {}


def get_or_create(session_id: str | None, user_id: str, kyc_level: int = 1) -> UserContext:
    sid = session_id or new_id("s_")
    with _lock:
        ctx = _sessions.get(sid)
        if ctx is None:
            ctx = UserContext(user_id=user_id, session_id=sid, kyc_level=kyc_level, trace_id=new_id("tr_"))
            _sessions[sid] = ctx
        elif ctx.user_id != user_id:
            # 会话归属不符 → 当作「不存在」。对外一律 404，不告诉调用方
            # 「这个 id 是别人的」，否则等于确认了它的存在。
            raise SessionOwnershipError(sid)
        # 每次请求刷新 trace_id，便于审计按「一轮对话」聚合
        ctx.trace_id = new_id("tr_")
        return ctx


def get(session_id: str) -> UserContext | None:
    return _sessions.get(session_id)


def clear(session_id: str) -> None:
    with _lock:
        _sessions.pop(session_id, None)
        _clarify.pop(session_id, None)


def set_clarify(session_id: str, intents: list[dict]) -> None:
    """记住「这一轮没问完的意图」，供下一轮补槽。"""
    with _lock:
        if intents:
            _clarify[session_id] = intents
        else:
            _clarify.pop(session_id, None)


def take_clarify(session_id: str) -> list[dict] | None:
    """取走并清空。只活一轮，避免上一轮的问句在十分钟后突然生效。"""
    with _lock:
        return _clarify.pop(session_id, None)


def audit_snapshot(ctx: UserContext) -> dict:
    return {
        "session_id": ctx.session_id,
        "user_id": ctx.user_id,
        "locked": ctx.locked,
        "denied_count": ctx.denied_count,
        "injection_hits": ctx.injection_hits,
        "verified_factors": list(ctx.verified_factors),
    }
