"""人工接管 —— 自动流程处理不了时，把「交接棒」交给人。

赛题要求的「人工接管」不是一句「已为您转接客服」，那只是想让你觉得有人在。
真的接管需要三样东西：
  1. 一张**可追溯**的工单（谁、什么时候、为什么）
  2. 把这次会话的**完整审计链**一路带过去，客服不用再问用户「您刚才点了什么」
  3. 把会话里所有未完成的待确认操作**全部作废**——交接期间悬着的授权是风险
"""

from __future__ import annotations

import sqlite3

from app.bank.db import new_id, now_iso
from app.engine import pending

# 能触发转人工的说法。用户主动说，系统就接——不做二次挽留。
HANDOFF_REASON = "用户主动请求转人工"


def open_ticket(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    session_id: str,
    trace_id: str,
    reason: str = HANDOFF_REASON,
) -> dict:
    # ★ 按 session 统计，不是按 trace：trace_id 是「一轮对话」的粒度，
    #   按 trace 数只会得到 1 条，工单上那句「完整审计链」就成了一句假话。
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE session_id=?", (session_id,)
    ).fetchone()["n"]
    tid = new_id("ho_")
    conn.execute(
        "INSERT INTO handoff_tickets(id,user_id,session_id,trace_id,reason,audit_rows,"
        "status,created_at) VALUES (?,?,?,?,?,?,'open',?)",
        (tid, user_id, session_id, trace_id, reason, int(rows), now_iso()),
    )
    # 交接期间不留悬空授权
    cancelled = pending.cancel_all(conn, session_id)
    return {
        "ticket_id": tid,
        "session_id": session_id,
        "trace_id": trace_id,
        "reason": reason,
        "audit_rows": int(rows),
        "cancelled_confirmations": cancelled,
    }


def render_ticket(t: dict) -> str:
    tail = (
        f"\n   同时已作废本次会话中 {t['cancelled_confirmations']} 个尚未确认的操作，"
        f"交接期间不会有任何自动执行。"
        if t["cancelled_confirmations"] else ""
    )
    return (
        f"🧑‍💼 已为您转接人工客服，工单号 {t['ticket_id']}（事由：{t['reason']}）。\n"
        f"   本次会话的完整审计链（session {t['session_id']}，共 {t['audit_rows']} 条记录）"
        f"已附在工单上，客服能直接看到每一步操作与每一次裁决，您不必复述。" + tail
    )
