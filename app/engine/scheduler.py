"""预约任务调度器 —— 让「明天上午转 300」真的在那天发生。

★ 为什么必须补上这个空洞：
   定时转账在没有调度器时只是台账里的一行：不扣款、不写流水、按执行日占额度，
   但**到期什么都不会发生**。2026-10-30 之前文档里如实写着「演示环境未开启调度器」——
   那是一句诚实的缺口，也是一个功能上的空洞：用户预约了，钱永远转不出去。

★ 三条硬要求（不是「顺手实现」，是设计约束）：

   1. **幂等**：同一行只能被执行一次。取任务与状态更新在同一个事务里完成，
      SQLite 的写串行化保证两个调度进程不会各扣一次钱。
      重复执行是本模块最危险的失败模式——它不会报错，只会多转一笔。

   2. **允许补跑，但只在到期之后**：调度器停过一天，第二天启动要把昨天的一并做掉；
      但绝不能提前执行——预约日期是用户的意思表示，不是建议。

   3. **失败必须留痕**：到期时余额不足不能静默跳过，要记成 `failed` 并写审计。
      否则用户以为钱转了，而账上什么都没发生——这比直接拒绝更糟。

★ 状态机（transfer_ledger.status）：
       scheduled ──到期且余额足够──> executed
               └──到期但余额不足──> failed
   `failed` 不占限额，`scheduled` 与 `executed` 都占（见 db.daily_transferred_cents）。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import date

from app.bank.db import (
    audit,
    connect,
    get_account,
    new_id,
    now_iso,
)

__all__ = ["run_due_transfers", "start_background", "due_transfers"]


def due_transfers(conn: sqlite3.Connection, today: str | None = None) -> list[sqlite3.Row]:
    """到期应执行的预约转账（ts 是执行日，不是创建日）。"""
    today = today or date.today().isoformat()
    return conn.execute(
        """SELECT * FROM transfer_ledger
           WHERE status='scheduled' AND substr(ts,1,10) <= ?
           ORDER BY ts, id""",
        (today,),
    ).fetchall()


def _execute_one(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """执行单笔预约转账。返回 True 表示已扣款。

    ★ 这里刻意不用 `status='executing'` 这种中间态：
      多一个状态就多一条「崩在中间怎么办」的路径，而中间态不占额度、
      等于给拆单开了个窗口。取任务和扣款放在同一个事务里，
      由 SQLite 的写锁负责互斥，比状态机更少出错。
    """
    acc = get_account(conn, row["user_id"], "current")
    if acc is None or int(acc["balance_cents"]) < int(row["amount_cents"]):
        conn.execute("UPDATE transfer_ledger SET status='failed' WHERE id=?", (row["id"],))
        audit(
            conn,
            trace_id=row["trace_id"] or new_id("sch_"),
            stage="scheduled_transfer_failed",
            user_id=row["user_id"],
            payload={
                "ledger_id": row["id"],
                "amount_cents": row["amount_cents"],
                "counterparty": row["counterparty"],
                "reason": "账户不存在" if acc is None else "余额不足",
                "balance_cents": int(acc["balance_cents"]) if acc is not None else None,
                "due_date": row["ts"][:10],
            },
        )
        return False

    conn.execute(
        "UPDATE accounts SET balance_cents = balance_cents - ? WHERE id=?",
        (row["amount_cents"], acc["id"]),
    )
    conn.execute(
        "INSERT INTO transactions(id,account_id,user_id,ts,direction,amount_cents,"
        "counterparty,category,memo) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            new_id("t_"),
            acc["id"],
            row["user_id"],
            now_iso(),
            "out",
            row["amount_cents"],
            row["counterparty"],
            "转账",
            "预约转账（到期自动执行）",
        ),
    )
    conn.execute("UPDATE transfer_ledger SET status='executed' WHERE id=?", (row["id"],))
    audit(
        conn,
        trace_id=row["trace_id"] or new_id("sch_"),
        stage="scheduled_transfer_executed",
        user_id=row["user_id"],
        payload={
            "ledger_id": row["id"],
            "amount_cents": row["amount_cents"],
            "counterparty": row["counterparty"],
            "due_date": row["ts"][:10],
            "balance_after_cents": int(acc["balance_cents"]) - int(row["amount_cents"]),
        },
    )
    return True


def run_due_transfers(today: str | None = None) -> dict:
    """扫一遍到期预约并执行，返回 {executed, failed, skipped}。

    整个扫描在一个事务里：中途抛异常就整批回滚，
    不会出现「扣了钱但台账还是 scheduled」这种半截状态。
    """
    today = today or date.today().isoformat()
    executed: list[dict] = []
    failed: list[dict] = []

    with connect() as conn:
        for row in due_transfers(conn, today):
            # 事务内再确认一次状态：并发调用时对方可能已经处理完
            fresh = conn.execute(
                "SELECT status FROM transfer_ledger WHERE id=?", (row["id"],)
            ).fetchone()
            if fresh is None or fresh["status"] != "scheduled":
                continue
            ok = _execute_one(conn, row)
            item = {
                "ledger_id": row["id"],
                "user_id": row["user_id"],
                "counterparty": row["counterparty"],
                "amount_cents": int(row["amount_cents"]),
                "due_date": row["ts"][:10],
            }
            (executed if ok else failed).append(item)

    return {
        "date": today,
        "executed": executed,
        "failed": failed,
        "count": len(executed) + len(failed),
    }


def start_background(interval_s: float = 60.0) -> threading.Thread:
    """后台每分钟扫一次。由 app.main 在 JH_SCHEDULER=1 时启动。

    ★ 为什么默认不启动：演示、pytest、对抗评估必须是可复现的，
      一个后台线程会在用例中途改数据库，把「这次通过」变成运气。
      `run.py` 会显式打开它，测试永远不会。
    """
    stop = threading.Event()

    def _loop() -> None:
        while not stop.wait(interval_s):
            try:
                result = run_due_transfers()
                if result["count"]:
                    print(
                        f"[scheduler] {result['date']} 执行 {len(result['executed'])} 笔、"
                        f"失败 {len(result['failed'])} 笔"
                    )
            except Exception as exc:  # noqa: BLE001  调度线程绝不能把服务带崩
                print(f"[scheduler] 本轮跳过：{exc}")

    t = threading.Thread(target=_loop, name="jinheng-scheduler", daemon=True)
    t.start()
    return t


def _cli() -> int:
    """python -m app.engine.scheduler [--once] [--date YYYY-MM-DD]"""
    import json
    import sys

    args = sys.argv[1:]
    today = None
    if "--date" in args:
        today = args[args.index("--date") + 1]
    if "--once" in args or "--date" in args:
        out = run_due_transfers(today)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    interval = float(os.getenv("JH_SCHEDULER_INTERVAL", "60"))
    print(f"[scheduler] 每 {interval:g} 秒扫一次，Ctrl+C 退出")
    try:
        while True:
            out = run_due_transfers()
            if out["count"]:
                print(json.dumps(out, ensure_ascii=False))
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
