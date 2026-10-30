"""HTTP 接口。前端 / IM 适配层都接到这里。"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app import auth
from app.bank import sms
from app.bank.db import (
    audit,
    audit_trail_by_session,
    check_user_pin,
    connect,
    daily_transferred_cents,
    verify_audit_chain,
)
from app.config import settings
from app.engine import circuit, session
from app.engine.orchestrator import Orchestrator
from app.tools.base import REGISTRY

router = APIRouter()
_orchestrator = Orchestrator()


# ------------------------------------------------------------------ 身份

def current_user(authorization: str | None) -> str:
    """从 `Authorization: Bearer <token>` 得出**服务端认定**的用户。

    ★ 这是整个 API 的身份边界：调用方塞 `user_id` 不再有任何作用。
      修之前 `{"user_id":"u_2001","message":"我有哪些卡"}` 能直接读到张伟的卡——
      数据库层每张表都带 user_id 条件，但「谁是 u_2001」由调用方说了算，
      等于越权防线建在沙子上。
    """
    uid = auth.verify_token(auth.bearer(authorization))
    if not uid:
        raise HTTPException(
            401, "未登录或令牌已失效。先 POST /api/auth/login 换取令牌，"
                 "再以 `Authorization: Bearer <token>` 访问。",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return uid


def _require(have: str, authorization: str | None) -> str:
    """取当前用户，并保证它等于 have（用于「只能查自己的会话」）。"""
    uid = current_user(authorization)
    if uid != have:
        raise HTTPException(403, "只能访问自己的数据")
    return uid


class LoginIn(BaseModel):
    user_id: str = Field(min_length=1, max_length=64, examples=["u_1001"])
    pin: str = Field(min_length=4, max_length=32, examples=["123456"])


class LoginOut(BaseModel):
    token: str
    user_id: str
    name: str
    kyc_level: int
    expires_in: int


class ChatIn(BaseModel):
    # ★ 保留这个字段只为兼容老调用方：带令牌时它必须为空或与令牌一致，
    #   否则 403。身份一律以令牌为准。
    user_id: str | None = Field(default=None, description="可选。带令牌时以令牌为准；与令牌不一致将被拒绝")
    session_id: str | None = Field(
        default=None,
        description="留空则开新会话；多轮对话（确认 / 输验证码 / 取消）务必带上上一轮返回的 session_id",
    )
    message: str = Field(min_length=1, max_length=500)


class ChatOut(BaseModel):
    session_id: str
    text: str
    trace_id: str
    executed: list[dict] = []
    denied: list[dict] = []
    pending: list[dict] = []
    # ★ 被跳过的步骤（条件不成立 / 前置被跳过）。不暴露这个字段，
    #   API 调用方会以为「没执行 = 系统坏了」，而其实是条件正常生效。
    skipped: list[dict] = []
    guard_hits: list[str] = []
    locked: bool = False
    # ★ 这一轮是不是在追问（缺槽位 / 需要澄清）。调用方不必再去回复文案里猜。
    clarify: list[str] = []


@router.get("/health", tags=["运维"])
def health() -> dict:
    return {"ok": True, "service": "jinheng", "llm_mode": settings.llm_mode}


@router.post("/auth/login", response_model=LoginOut, tags=["认证"])
def login(body: LoginIn) -> LoginOut:
    """用口令换令牌。令牌签名覆盖「用户 + 有效期」，调用方改不动也伪造不了。

    演示账号：`u_1001 / 123456`；越权对照账号 `u_2001 / 654321`。
    """
    wait = auth.login_blocked(body.user_id)
    if wait > 0:
        raise HTTPException(429, f"登录失败次数过多，请 {int(wait) + 1} 秒后再试")

    with connect() as conn:
        ok = check_user_pin(conn, body.user_id, body.pin)
        if not ok:
            n = auth.record_login_failure(body.user_id)
            # ★ 统一话术，不区分「用户不存在」和「口令错误」——
            #   区分了就等于送出一个用户名枚举接口。
            raise HTTPException(401, f"账号或口令不正确（第 {n} 次失败，连续 {auth._MAX_FAILS} 次将锁定）")
        auth.clear_login_failures(body.user_id)
        row = conn.execute("SELECT * FROM users WHERE id=?", (body.user_id,)).fetchone()
        audit(conn, trace_id=f"login_{body.user_id}", stage="auth",
              payload={"event": "login_ok"}, user_id=body.user_id)

    token = auth.issue_token(body.user_id)
    return LoginOut(
        token=token, user_id=body.user_id, name=row["name"],
        kyc_level=int(row["kyc_level"]), expires_in=auth.TOKEN_TTL_S,
    )


@router.get("/auth/me", tags=["认证"])
def whoami(authorization: str | None = Header(default=None)) -> dict:
    uid = current_user(authorization)
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return {
        "user_id": uid,
        "name": row["name"] if row else "",
        "kyc_level": int(row["kyc_level"]) if row else 0,
        # ★ 角色来自数据库，不由任何一句话决定
        "role": (row["role"] if row and "role" in row.keys() else "customer"),
    }


@router.get("/auth/sms", tags=["认证"])
def sms_inbox(limit: int = 10, authorization: str | None = Header(default=None)) -> dict:
    """模拟「你手里那部手机」的短信收件箱。

    ★ 验证码**不经过聊天回复**：聊天通道正是攻击者能改写的那条通道。
      演示时把这里的结果显示在页面右侧的「📱 手机」面板里——
      看得到的和能从对话里读到的是两回事，这正是双因子认证的意义。
    """
    uid = current_user(authorization)
    with connect() as conn:
        items = sms.inbox(conn, uid, limit=limit)
    return {"user_id": uid, "count": len(items), "messages": items}


@router.get("/tools", tags=["能力"])
def list_tools() -> list[dict]:
    return [
        {
            "name": m.name,
            "label": m.label,
            "base_level": m.base_level.value,
            "is_fund_movement": m.is_fund_movement,
            "requires_contact_resolution": m.resolves_contact,
            "min_kyc": m.min_kyc,
            "has_precheck": m.precheck is not None,
            "description": m.description,
        }
        for m in REGISTRY.values()
    ]


@router.get("/capabilities", tags=["能力"])
def capabilities() -> dict:
    """机器可读的能力清单：工具、场景覆盖、架构保证。

    自动评测脚本从这里拿「这个作品声称会做什么」，不用去猜。
    内容直接读工具注册表，与 /tools 同源，不存在两份清单对不上的问题。
    """
    try:
        from capabilities import manifest
    except ImportError as exc:  # noqa: BLE001
        # 只可能发生在「不是从工程根目录启动」的情况下，如实说清楚比 500 好
        raise HTTPException(
            503, f"能力清单模块不可用（请从工程根目录启动）：{exc}"
        ) from exc
    return manifest()


@router.post("/chat", response_model=ChatOut, tags=["对话"])
def chat(body: ChatIn, authorization: str | None = Header(default=None)) -> ChatOut:
    uid = current_user(authorization)
    if body.user_id and body.user_id != uid:
        raise HTTPException(
            403, "身份以令牌为准：不需要、也不允许在请求体里指定其他用户。"
        )
    r = _orchestrator.handle_message(uid, body.session_id, body.message)
    return ChatOut(
        session_id=r.session_id, text=r.text, trace_id=r.trace_id,
        executed=r.executed, denied=r.denied, pending=r.pending,
        skipped=r.skipped, guard_hits=r.guard_hits, locked=r.locked,
        clarify=r.clarify,
    )


@router.get("/audit/verify", tags=["审计"])
def audit_verify(authorization: str | None = Header(default=None)) -> dict:
    """逐行重算哈希链，回答「这份审计记录有没有被改过」。

    ★ 「可审计」≠「不可篡改」。一张普通的表，有库权限就能改，改完还看不出来。
      每行咬着上一行之后，改任意一行，后面的 prev_hash 全部对不上——
      一个 GET 就能把口号变成证据。

    ★ 路由必须声明在 `/audit/{trace_id}` **之前**，否则 verify 会被当成 trace_id 吞掉。
    """
    current_user(authorization)
    with connect() as conn:
        result = verify_audit_chain(conn)
        result["total_rows"] = conn.execute("SELECT COUNT(*) AS c FROM audit_log").fetchone()["c"]
    return result


@router.get("/audit/{trace_id}", tags=["审计"])
def audit_by_trace(trace_id: str, authorization: str | None = Header(default=None)) -> dict:
    uid = current_user(authorization)
    trail = Orchestrator.trail(trace_id)
    # ★ 存在但不是你的 → 和不存在一样回 404。
    #   回 403 等于告诉对方「这个 trace 是存在的」，就是一次存在性探测。
    if not trail:
        raise HTTPException(404, "未找到该 trace 的审计记录")
    with connect() as conn:
        owner = conn.execute(
            "SELECT user_id FROM audit_log WHERE trace_id=? AND user_id IS NOT NULL LIMIT 1",
            (trace_id,),
        ).fetchone()
    if owner is not None and owner["user_id"] != uid:
        raise HTTPException(404, "未找到该 trace 的审计记录")
    return {"trace_id": trace_id, "trail": trail}


@router.get("/session/{session_id}", tags=["运维"])
def session_state(session_id: str, authorization: str | None = Header(default=None)) -> dict:
    ctx = session.get(session_id)
    if ctx is None:
        raise HTTPException(404, "会话不存在")
    _require(ctx.user_id, authorization)
    with connect() as conn:
        daily = daily_transferred_cents(conn, ctx.user_id)
    return {
        **session.audit_snapshot(ctx),
        "circuit": circuit.status(ctx),
        "daily_transferred_cents": daily,
    }


@router.get("/session/{session_id}/audit", tags=["审计"])
def session_audit(
    session_id: str, limit: int = 200, authorization: str | None = Header(default=None)
) -> dict:
    """整段会话的审计链，按时间正序。

    ★ 为什么需要它：`trace_id` 是「一轮对话」的粒度，一次多轮操作会散成十几条 trace。
      人工接管工单、安全自评报告、复现某次事故，要的都是「整段会话」而不是「某一轮」。
    """
    uid = current_user(authorization)
    with connect() as conn:
        trail = audit_trail_by_session(conn, session_id, limit=limit)
    if not trail:
        raise HTTPException(404, "该会话没有任何审计记录")
    owners = {t.get("user_id") for t in trail if t.get("user_id")}
    if owners and owners != {uid}:
        raise HTTPException(404, "该会话没有任何审计记录")
    return {"session_id": session_id, "count": len(trail), "trail": trail}
