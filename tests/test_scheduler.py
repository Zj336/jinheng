"""预约转账调度器。

★ 这组用例盯的不是「功能能跑」，而是三种会**安静地出错**的情形：
  1. 同一笔被执行两次 —— 不报错，只是多转了一笔钱。
  2. 没到期就执行 —— 用户预约的是那天，不是「现在也行」。
  3. 到期余额不足却被静默跳过 —— 用户以为转了，账上什么都没发生。
"""

from datetime import date, timedelta

from app.bank.db import connect, get_account, new_id, now_iso
from app.engine.scheduler import run_due_transfers


def _schedule(conn, due: date, cents: int, name: str = "王强") -> str:
    """造一条预约转账台账（ts = 执行日）。"""
    contact = conn.execute(
        "SELECT id FROM contacts WHERE user_id='u_1001' AND name=?", (name,)
    ).fetchone()
    lid = new_id("l_")
    conn.execute(
        "INSERT INTO transfer_ledger(id,user_id,ts,amount_cents,contact_id,counterparty,"
        "status,decision_level,trace_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (lid, "u_1001", f"{due.isoformat()}T00:00:00", cents,
         contact["id"] if contact else None, name, "scheduled", "", new_id("tr_")),
    )
    return lid


def _status(conn, lid: str) -> str:
    return conn.execute(
        "SELECT status FROM transfer_ledger WHERE id=?", (lid,)
    ).fetchone()["status"]


def test_due_scheduled_transfer_is_executed():
    with connect() as conn:
        before = int(get_account(conn, "u_1001", "current")["balance_cents"])
        lid = _schedule(conn, date.today(), 30_000)

    out = run_due_transfers()
    assert out["count"] == 1
    assert len(out["executed"]) == 1

    with connect() as conn:
        after = int(get_account(conn, "u_1001", "current")["balance_cents"])
        assert _status(conn, lid) == "executed"
        tx = conn.execute(
            "SELECT * FROM transactions WHERE user_id='u_1001' AND amount_cents=30000 "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert before - after == 30_000
    assert tx is not None and tx["direction"] == "out"


def test_scheduler_is_idempotent():
    """跑两遍只能扣一次钱。这是本模块最危险的失败模式：它不会报错。"""
    with connect() as conn:
        lid = _schedule(conn, date.today(), 20_000)
        before = int(get_account(conn, "u_1001", "current")["balance_cents"])

    run_due_transfers()
    run_due_transfers()
    run_due_transfers()

    with connect() as conn:
        after = int(get_account(conn, "u_1001", "current")["balance_cents"])
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM transactions WHERE amount_cents=20000"
        ).fetchone()["c"]
        assert _status(conn, lid) == "executed"
    assert before - after == 20_000
    assert n == 1


def test_future_scheduled_transfer_is_not_executed():
    """没到期就是不能动。预约日期是用户的意思表示，不是建议。"""
    with connect() as conn:
        lid = _schedule(conn, date.today() + timedelta(days=2), 10_000)

    out = run_due_transfers()
    assert out["count"] == 0

    with connect() as conn:
        assert _status(conn, lid) == "scheduled"


def test_overdue_transfer_is_caught_up():
    """调度器停过几天，重启后要把逾期的补上，而不是跳过。"""
    with connect() as conn:
        lid = _schedule(conn, date.today() - timedelta(days=3), 10_000)

    out = run_due_transfers()
    assert out["count"] == 1
    with connect() as conn:
        assert _status(conn, lid) == "executed"


def test_insufficient_balance_marks_failed_with_audit():
    """到期余额不足：不能静默跳过，必须记 failed 并留审计。"""
    with connect() as conn:
        acc = get_account(conn, "u_1001", "current")
        lid = _schedule(conn, date.today(), int(acc["balance_cents"]) + 100_000)
        before = int(acc["balance_cents"])

    out = run_due_transfers()
    assert out["count"] == 1
    assert len(out["failed"]) == 1
    assert out["executed"] == []

    with connect() as conn:
        after = int(get_account(conn, "u_1001", "current")["balance_cents"])
        assert _status(conn, lid) == "failed"
        row = conn.execute(
            "SELECT * FROM audit_log WHERE stage='scheduled_transfer_failed' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert after == before          # 一分钱都没动
    assert row is not None
    assert "余额不足" in row["payload"]


def test_failed_transfer_does_not_occupy_quota():
    """失败的不占额度，否则「转过一次失败」会让后面几天都转不出钱。"""
    with connect() as conn:
        acc = get_account(conn, "u_1001", "current")
        _schedule(conn, date.today(), int(acc["balance_cents"]) + 100_000)

    run_due_transfers()

    from app.bank.db import daily_transferred_cents

    with connect() as conn:
        assert daily_transferred_cents(conn, "u_1001") == 0
