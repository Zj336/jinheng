"""会话身份：令牌，不是请求体里的一个字符串。

★ 为什么必须补这一层（2026-10-30 实跑查出的洞）：
  此前 `/api/chat` 的用户是谁，由调用方在请求体里写 `user_id` 决定。
  数据库层确实每张表都带 `user_id` 条件、查不出别人的数据——但只要
  把请求体里的 `user_id` 换成别人的，就等于用自己的身份读别人的账。
  实测：`{"user_id":"u_2001","message":"我有哪些卡"}` 直接返回了张伟的农行卡。
  这不是「越权查询没做」，而是「身份由调用方声明」——最经典的一类鉴权缺陷。

  正确的边界是：**服务端从令牌里读用户，调用方说不着。**
  令牌由 /api/auth/login 用口令换发，签名覆盖用户与有效期，改一个字节就作废。

★ 三层身份，别混为一谈（答辩时要能分开讲）：
  1. 会话身份（本模块）——「你是谁」，登录一次，令牌承载；
  2. 操作授权（policy.py 三色）——「这一笔要不要再验一次」；
  3. 数据归属（db.py 每张表带 user_id）——「你只能看见自己的」。
  只有 2 没有 1，等于门锁很高级但门框是纸糊的。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from pathlib import Path

from app.config import settings

TOKEN_TTL_S = int(os.getenv("JH_TOKEN_TTL", str(2 * 3600)))   # 2 小时
_PBKDF2_ROUNDS = 120_000

# 登录失败节流：同一用户 10 分钟内错 5 次即锁定一小段时间。
# ★ 没有它，6 位口令可以在几分钟内被穷举——前面三色分级做得再细也没意义。
_MAX_FAILS = 5
_LOCK_WINDOW_S = 600
_fails: dict[str, list[float]] = {}


# ------------------------------------------------------------------ 密钥

_secret_cache: bytes | None = None


def _secret() -> bytes:
    """签名密钥。优先读环境变量；否则在 data/ 下生成一次并复用。

    ★ 为什么要落文件而不是每次启动随机生成：
      随机生成的话，服务一重启（含 --reload 每次改代码）所有人的令牌全失效，
      演示中途刷新页面就被踢回登录页——体验上会被当成 bug。
      写进 data/ 只用于演示环境；生产应当走 KMS/环境变量，见 README 的环境变量表。
    """
    global _secret_cache
    if _secret_cache is not None:
        return _secret_cache

    env = os.getenv("JH_AUTH_SECRET", "").strip()
    if env:
        _secret_cache = env.encode()
        return _secret_cache

    path = Path(settings.db_path).parent / ".auth_secret"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raw = path.read_text(encoding="utf-8").strip()
        if raw:
            _secret_cache = bytes.fromhex(raw)
            return _secret_cache
    raw = secrets.token_bytes(32)
    path.write_text(raw.hex(), encoding="utf-8")
    _secret_cache = raw
    return _secret_cache


# ------------------------------------------------------------------ 口令

def new_salt() -> str:
    return secrets.token_hex(16)


def hash_pin(pin: str, salt: str) -> str:
    """口令哈希。用 PBKDF2 而不是裸 sha256——裸哈希在 6 位纯数字上等于明文。"""
    return hashlib.pbkdf2_hmac(
        "sha256", pin.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ROUNDS
    ).hex()


def check_pin(pin: str, salt: str | None, digest: str | None) -> bool:
    if not salt or not digest:
        return False
    return hmac.compare_digest(hash_pin(pin, salt), digest)


# ------------------------------------------------------------------ 令牌

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_token(user_id: str, ttl_s: int = TOKEN_TTL_S) -> str:
    now = int(time.time())
    payload = {"sub": user_id, "iat": now, "exp": now + ttl_s, "jti": uuid.uuid4().hex[:12]}
    body = _b64e(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
    sig = _b64e(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_token(token: str | None) -> str | None:
    """校验签名与有效期，返回 user_id；任何一处不对都返回 None（fail-closed）。"""
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    expect = _b64e(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expect):
        return None
    try:
        payload = json.loads(_b64d(body))
    except Exception:  # noqa: BLE001  畸形令牌一律当无效，不做任何猜测
        return None
    if not isinstance(payload, dict):
        return None
    try:
        if int(payload.get("exp", 0)) <= int(time.time()):
            return None
    except (TypeError, ValueError):
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) and sub else None


def bearer(header_value: str | None) -> str | None:
    """从 `Authorization: Bearer xxx` 里取出令牌本身。"""
    if not header_value:
        return None
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


# ------------------------------------------------------------------ 登录节流

def login_blocked(user_id: str) -> float:
    """返回还需等待的秒数；0 表示没被锁。"""
    now = time.time()
    marks = [t for t in _fails.get(user_id, []) if now - t < _LOCK_WINDOW_S]
    _fails[user_id] = marks
    if len(marks) >= _MAX_FAILS:
        return max(0.0, _LOCK_WINDOW_S - (now - marks[0]))
    return 0.0


def record_login_failure(user_id: str) -> int:
    now = time.time()
    marks = [t for t in _fails.get(user_id, []) if now - t < _LOCK_WINDOW_S]
    marks.append(now)
    _fails[user_id] = marks
    return len(marks)


def clear_login_failures(user_id: str) -> None:
    _fails.pop(user_id, None)
