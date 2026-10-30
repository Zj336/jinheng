"""策略引擎单测 —— 直接测裁定，不经过 LLM。"""

from datetime import date

from app.bank.db import connect
from app.engine.policy import PolicyEngine
from app.schemas import DecisionOutcome, Plan, RiskLevel, Step, UserContext


def _ctx(**kw) -> UserContext:
    base = dict(user_id="u_1001", session_id="t", kyc_level=2)
    base.update(kw)
    return UserContext(**base)


def _plan(*steps: Step) -> Plan:
    return Plan(steps=list(steps))


def test_green_query_is_auto_allowed():
    engine = PolicyEngine()
    plan = _plan(Step(id="s1", tool="query_balance", args={"account_kind": "current"}))
    with connect() as conn:
        d = engine.adjudicate_plan(conn, plan, _ctx())[0]
    assert d.level == RiskLevel.GREEN
    assert d.outcome == DecisionOutcome.ALLOW


def test_first_time_counterparty_escalates():
    engine = PolicyEngine()
    plan = _plan(Step(id="s1", tool="transfer", args={"to": "王强", "amount_cents": 30_000}))
    with connect() as conn:
        d = engine.adjudicate_plan(conn, plan, _ctx())[0]
    assert d.level == RiskLevel.RED
    assert "P-041" in d.policy_hits


def test_daily_cumulative_over_limit_is_denied_not_escalated():
    """★ 核心：限额是硬约束，不是风险信号。

    当日已有 600，再转 900 就是 1500 > 1000。修之前这里只是把等级升到 RED，
    而 RED 可以被短信验证码放行 —— 也就是说 1000 元的日限额输个验证码就能突破。
    2026-10-30 用户实跑时正是这么把限额顶穿的，所以这个用例改判 DENY。
    """
    engine = PolicyEngine()
    ctx = _ctx(daily_transferred_cents=60_000)
    plan = _plan(Step(id="s1", tool="transfer", args={"to": "王强", "amount_cents": 90_000}))
    with connect() as conn:
        d = engine.adjudicate_plan(conn, plan, ctx)[0]
    assert d.outcome == DecisionOutcome.DENY
    assert "P-040" in d.policy_hits
    assert "剩余可转" in d.reason


def test_cumulative_across_steps_in_same_plan():
    """★ 同一计划内两笔也要累计，不能各判各的。"""
    engine = PolicyEngine()
    plan = _plan(
        Step(id="s1", tool="transfer", args={"to": "王强", "amount_cents": 60_000}),
        Step(id="s2", tool="transfer", args={"to": "王强", "amount_cents": 60_000}, depends_on=["s1"]),
    )
    with connect() as conn:
        ds = engine.adjudicate_plan(conn, plan, _ctx())
    assert ds[1].outcome == DecisionOutcome.DENY
    assert "P-040" in ds[1].policy_hits


def test_monthly_limit_is_a_hard_cap():
    engine = PolicyEngine(monthly_limit_cents=100_000)
    plan = _plan(Step(id="s1", tool="transfer", args={"to": "王强", "amount_cents": 90_000}))
    with connect() as conn:
        conn.execute(
            "INSERT INTO transfer_ledger(id,user_id,ts,amount_cents,status) VALUES (?,?,?,?,?)",
            ("tl_month", "u_1001", date.today().isoformat() + "T10:00:00", 50_000, "executed"),
        )
        d = engine.adjudicate_plan(conn, plan, _ctx())[0]
    assert d.outcome == DecisionOutcome.DENY
    assert "P-042" in d.policy_hits


def test_single_over_threshold_needs_strong_auth():
    """单笔 ≥ 500 元升红：不是拒绝，是加一道身份确认。"""
    engine = PolicyEngine()
    plan = _plan(Step(id="s1", tool="transfer", args={"to": "王强", "amount_cents": 60_000}))
    with connect() as conn:
        d = engine.adjudicate_plan(conn, plan, _ctx())[0]
    assert d.outcome == DecisionOutcome.MFA
    assert "P-044" in d.policy_hits


def test_unknown_tool_is_denied():
    engine = PolicyEngine()
    plan = _plan(Step(id="s1", tool="drop_database", args={}))
    with connect() as conn:
        d = engine.adjudicate_plan(conn, plan, _ctx())[0]
    assert d.outcome == DecisionOutcome.DENY
    assert "P-001" in d.policy_hits


def test_invalid_args_are_denied():
    engine = PolicyEngine()
    plan = _plan(Step(id="s1", tool="transfer", args={"to": "王强", "amount_cents": -1}))
    with connect() as conn:
        d = engine.adjudicate_plan(conn, plan, _ctx())[0]
    assert d.outcome == DecisionOutcome.DENY
    assert "P-010" in d.policy_hits


def test_cross_user_contact_is_not_reachable():
    """跨用户联系人不可见——越权防线。"""
    engine = PolicyEngine()
    plan = _plan(Step(id="s1", tool="transfer", args={"to": "c_stranger", "amount_cents": 100}))
    with connect() as conn:
        d = engine.adjudicate_plan(conn, plan, _ctx())[0]
    assert d.outcome == DecisionOutcome.DENY
    assert "P-011" in d.policy_hits


def test_locked_session_denies_everything():
    engine = PolicyEngine()
    plan = _plan(Step(id="s1", tool="query_balance", args={}))
    with connect() as conn:
        d = engine.adjudicate_plan(conn, plan, _ctx(locked=True))[0]
    assert d.outcome == DecisionOutcome.DENY
    assert "P-000" in d.policy_hits


def test_verified_factor_is_one_shot():
    engine = PolicyEngine()
    plan = _plan(Step(id="s1", tool="transfer", args={"to": "王强", "amount_cents": 30_000}))
    with connect() as conn:
        allowed = engine.adjudicate_plan(conn, plan, _ctx(verified_factors=["mfa"]))[0]
        # 凭证被消费后（orchestrator 会清空），同样的计划又需要验证
        again = engine.adjudicate_plan(conn, plan, _ctx())[0]
    assert allowed.outcome == DecisionOutcome.ALLOW
    assert again.outcome == DecisionOutcome.MFA
