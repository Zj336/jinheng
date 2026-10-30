"""待确认 / 待验证队列。

落库而不是放内存，理由：确认动作必须可审计、可追溯、可超时失效。
一条待确认记录 = 一次「用户即将授权的资金动作」，它必须留下痕迹。

★ 多因子验证不是「校验格式」，是「校验值」：
  每次发起强验证时生成一次性动态码，只有**持有该码**的人才能放行。
  这模拟的是短信验证码 / U 盾挑战码，而不是「只要说个六位数就行」。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from app.bank.db import new_id, now_iso

TTL_MINUTES = 10


def new_mfa_code() -> str:
    """一次性动态码。用 secrets 而非 random，避免被预测。"""
    return f"{secrets.randbelow(900000) + 100000}"


def save(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    user_id: str,
    trace_id: str,
    step_id: str,
    kind: str,
    steps: list[dict[str, Any]],
    code: str | None = None,
    results: dict[str, Any] | None = None,
) -> str:
    cid = new_id("cf_")
    expires = (datetime.now() + timedelta(minutes=TTL_MINUTES)).isoformat(timespec="seconds")
    payload: dict[str, Any] = {"steps": steps}
    if code:
        payload["code"] = code
    if results:
        # ★ 把「用户确认时所依据的执行结果」一并存下来。
        #   恢复执行时，条件步骤（如「余额够就转」）要用它来复算条件。
        #   不存的话，恢复后的计划里读不到前置结果，条件会一律判不成立，
        #   用户的确认就变成空授权——看起来成功，其实什么都没做。
        payload["results"] = results
    conn.execute(
        "INSERT INTO confirmations(id,trace_id,session_id,user_id,step_id,kind,payload,"
        "status,created_at,expires_at) VALUES (?,?,?,?,?,?,?,'pending',?,?)",
        (
            cid, trace_id, session_id, user_id, step_id, kind,
            json.dumps(payload, ensure_ascii=False), now_iso(), expires,
        ),
    )
    return cid


def find_active(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM confirmations WHERE session_id=? AND status='pending' AND expires_at>?"
        " ORDER BY created_at DESC LIMIT 1",
        (session_id, now_iso()),
    ).fetchone()


def mark(conn: sqlite3.Connection, confirmation_id: str, status: str) -> None:
    conn.execute("UPDATE confirmations SET status=? WHERE id=?", (status, confirmation_id))


def bump_attempt(conn: sqlite3.Connection, confirmation_id: str) -> int:
    """验证码输错累计次数；超过 3 次直接作废。"""
    row = conn.execute(
        "SELECT payload FROM confirmations WHERE id=?", (confirmation_id,)
    ).fetchone()
    data = json.loads(row["payload"])
    data["attempts"] = int(data.get("attempts", 0)) + 1
    conn.execute(
        "UPDATE confirmations SET payload=?, status=? WHERE id=?",
        (json.dumps(data, ensure_ascii=False),
         "void" if data["attempts"] >= 3 else "pending", confirmation_id),
    )
    return data["attempts"]


def load(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any]]:
    data = json.loads(row["payload"])
    return data.get("steps", []), data.get("code"), data.get("results", {})


def supersede(conn: sqlite3.Connection, session_id: str) -> int:
    """用户开始了新请求 → 之前悬着的授权一律作废，返回作废条数。

    ★ 与 cancel_all 的区别：
      cancel_all 是用户主动说「算了」；
      supersede 是用户根本没理上一个授权、直接说了别的事。
      两者都必须让旧授权失效——否则会留下一个「用户早就走开了、
      系统却还等着有人把那个验证码填进来」的敞口。
    ★ 状态记 'superseded' 而不是 'cancelled'：审计上要能分清
      「用户主动撤销」和「被后续请求顶掉」，这对复盘同样是关键信息。
    """
    cur = conn.execute(
        "UPDATE confirmations SET status='superseded' WHERE session_id=? AND status='pending'",
        (session_id,),
    )
    return cur.rowcount


def cancel_all(conn: sqlite3.Connection, session_id: str) -> int:
    """把本会话所有待确认/待验证的记录一次性作废，返回作废条数。

    ★ 必须作废**全部**而不是只作废最新一条：用户在中途连着发起过几次操作时，
      会话里可能同时存在多条 pending，只标记一条会把另外几条留成「还能被确认」，
      等于留了一个「用户以为已经取消、系统却仍会执行」的口子。
    """
    cur = conn.execute(
        "UPDATE confirmations SET status='cancelled' WHERE session_id=? AND status='pending'",
        (session_id,),
    )
    return cur.rowcount
