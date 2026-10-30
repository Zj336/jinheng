"""模拟短信网关 —— 动态码**不经过聊天通道**。

★ 起因（2026-10-30 用户实跑时的一句追问）：
  「冒充管理员这个发个验证码就通过，会不会太顺利了？」

  当时系统的做法是在**聊天回复里**打印「[模拟短信] 验证码：186355」。
  那等于把钥匙和锁放在同一个信封里：能改写对话的攻击者，
  也能从同一个窗口读到验证码。多因子认证在这里只是**看起来**存在。

★ 正确的形状是「双通道」：
     聊天通道（可被注入影响） ──▶ 提案
     手机通道（不在对话里）   ──▶ 动态码
  内核拿着两边的东西做校验。攻击者能污染前者，拿不到后者。

★ 还有一个常被忽略的点：**动态码绑定的不是「一句话」，而是那条待办记录**。
  验证码存在 confirmations 那一行上，行里还存着完整的计划（哪些步骤、给谁、多少钱）。
  所以「拿到 A 操作的验证码去放行 B 操作」在结构上不成立——见
  tests/test_security_hardening.py::test_code_is_bound_to_the_exact_operation

生产环境这里换成真实短信网关 / App 推送；本模块的 `inbox` 就代表「那部手机」。
"""

from __future__ import annotations

import sqlite3

from app.bank.db import connect, new_id, now_iso


def mask_phone(phone: str | None) -> str:
    p = (phone or "").strip()
    if len(p) < 7:
        return "绑定手机"
    return f"{p[:3]}****{p[-4:]}"


def send(
    conn: sqlite3.Connection,
    user_id: str,
    text: str,
    code: str | None = None,
    kind: str = "otp",
) -> dict:
    """投递一条短信。返回的记录里带 code，但调用方（编排层）**不得**把它放进回复文本。"""
    sid = new_id("sms_")
    ts = now_iso()
    conn.execute(
        "INSERT INTO sms_messages(id, user_id, ts, text, code, kind) VALUES (?,?,?,?,?,?)",
        (sid, user_id, ts, text, code, kind),
    )
    return {"id": sid, "ts": ts, "text": text, "code": code, "kind": kind}


def inbox(conn: sqlite3.Connection, user_id: str, limit: int = 20, with_code: bool = True) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM sms_messages WHERE user_id=? ORDER BY rowid DESC LIMIT ?",
        (user_id, max(1, min(limit, 100))),
    ).fetchall()
    out = []
    for r in rows:
        item = {"id": r["id"], "ts": r["ts"], "text": r["text"], "kind": r["kind"]}
        if with_code:
            item["code"] = r["code"]
        out.append(item)
    return out


def latest_code(user_id: str, kind: str = "otp") -> str | None:
    """取这个用户最近一条验证码。给 demo / 测试用（它们扮演「手里那部手机」）。"""
    with connect() as conn:
        row = conn.execute(
            "SELECT code FROM sms_messages WHERE user_id=? AND kind=? AND code IS NOT NULL "
            "ORDER BY rowid DESC LIMIT 1",
            (user_id, kind),
        ).fetchone()
    return row["code"] if row else None
