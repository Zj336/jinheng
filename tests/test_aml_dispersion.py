"""反洗钱 · 分散转出（P-045）。

★ 这一组用例回答的是一个具体问题：
  「把 5000 拆成 5 笔 900 转给不同的人，怎么防？」
  日限额防「一天拆几笔」、月限额防「天天拆」、收款人限额防「盯着一个人」——
  三条都不是为这个形状设计的。所以单独立一条规则，也单独验它。
"""

from datetime import date, timedelta

from app.bank.db import connect, new_id
from app.config import settings
from app.engine.policy import PolicyEngine
from app.schemas import DecisionOutcome, Plan, RiskLevel, Step, UserContext


def _ctx(**kw) -> UserContext:
    base = dict(user_id="u_1001", session_id="t", kyc_level=2)
    base.update(kw)
    return UserContext(**base)


def _plan(**args) -> Plan:
    return Plan(steps=[Step(id="s1", tool="transfer", args=args)])


def _ledger(conn, contact_id: str, name: str, cents: int, days_ago: int) -> None:
    """直接写台账，跳过策略引擎——我们要造的是「历史已经发生」的状态。"""
    ts = (date.today() - timedelta(days=days_ago)).isoformat() + "T10:00:00"
    conn.execute(
        "INSERT INTO transfer_ledger(id,user_id,ts,amount_cents,contact_id,counterparty,"
        "status,decision_level,trace_id) VALUES (?,?,?,?,?,?,?,?,?)",
        (new_id("l_"), "u_1001", ts, cents, contact_id, name, "executed", "RED", ""),
    )


def _contacts(conn, limit: int = 4) -> list[str]:
    rows = conn.execute(
        "SELECT id FROM contacts WHERE user_id='u_1001' ORDER BY name LIMIT ?", (limit,)
    ).fetchall()
    return [r["id"] for r in rows]


def test_dispersed_new_recipients_are_denied():
    """两个新收款人各转 900（近几天），再转第三个新收款人 → 触发 P-045。

    这正好是日限额拦不住的那种形状：每天只转一笔 900，单日不超限。
    """
    engine = PolicyEngine()
    with connect() as conn:
        ids = _contacts(conn, 3)
        names = [f"新收款人{i}" for i in range(3)]
        _ledger(conn, ids[0], names[0], 90_000, days_ago=4)
        _ledger(conn, ids[1], names[1], 90_000, days_ago=1)
        # 第三个人还没转过（first_time），本笔就是要转的
        to_name = conn.execute(
            "SELECT name FROM contacts WHERE id=?", (ids[2],)
        ).fetchone()["name"]
        d = engine.adjudicate_plan(
            conn, _plan(to=to_name, amount_cents=90_000), _ctx()
        )[0]

    assert d.outcome == DecisionOutcome.DENY
    assert "P-045" in d.policy_hits
    assert d.level == RiskLevel.RED


def test_repeated_payments_to_one_person_are_not_flagged():
    """同一个收款人转三次不算分散——那是常来往，不是结构化拆分。"""
    engine = PolicyEngine()
    with connect() as conn:
        ids = _contacts(conn, 1)
        name = conn.execute("SELECT name FROM contacts WHERE id=?", (ids[0],)).fetchone()["name"]
        _ledger(conn, ids[0], name, 60_000, days_ago=3)
        _ledger(conn, ids[0], name, 60_000, days_ago=2)
        d = engine.adjudicate_plan(
            conn, _plan(to=name, amount_cents=60_000), _ctx()
        )[0]

    assert "P-045" not in d.policy_hits


def test_old_recipients_fall_out_of_the_window():
    """窗口外（8 天前）的两个新收款人不该被算进来。"""
    engine = PolicyEngine()
    with connect() as conn:
        ids = _contacts(conn, 3)
        names = [f"旧收款人{i}" for i in range(3)]
        _ledger(conn, ids[0], names[0], 90_000, days_ago=settings.aml_window_days + 1)
        _ledger(conn, ids[1], names[1], 90_000, days_ago=settings.aml_window_days + 3)
        to_name = conn.execute(
            "SELECT name FROM contacts WHERE id=?", (ids[2],)
        ).fetchone()["name"]
        d = engine.adjudicate_plan(
            conn, _plan(to=to_name, amount_cents=90_000), _ctx()
        )[0]

    assert "P-045" not in d.policy_hits


def test_amount_below_threshold_is_not_flagged():
    """三个新收款人但合计金额很小（< 2000 元），属于正常生活场景，不该拦。"""
    engine = PolicyEngine()
    with connect() as conn:
        ids = _contacts(conn, 4)
        names = [f"小额收款人{i}" for i in range(4)]
        _ledger(conn, ids[0], names[0], 30_000, days_ago=5)
        _ledger(conn, ids[1], names[1], 30_000, days_ago=4)
        to_name = conn.execute(
            "SELECT name FROM contacts WHERE id=?", (ids[2],)
        ).fetchone()["name"]
        d = engine.adjudicate_plan(
            conn, _plan(to=to_name, amount_cents=30_000), _ctx()
        )[0]

    assert "P-045" not in d.policy_hits


def test_aml_denial_survives_sms_code():
    """P-045 与 P-040 同类：强验证不能覆盖。策略层拒绝就是最终结局。"""
    engine = PolicyEngine()
    with connect() as conn:
        ids = _contacts(conn, 3)
        _ledger(conn, ids[0], "甲", 90_000, days_ago=4)
        _ledger(conn, ids[1], "乙", 90_000, days_ago=2)
        to_name = conn.execute("SELECT name FROM contacts WHERE id=?", (ids[2],)).fetchone()["name"]
        ctx = _ctx()  # 即使带着一次性的强验证凭证，也不能放行
        d = engine.adjudicate_plan(conn, _plan(to=to_name, amount_cents=90_000), ctx)[0]

    assert d.outcome == DecisionOutcome.DENY
    assert "P-045" in d.policy_hits
