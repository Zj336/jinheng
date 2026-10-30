"""跨场景联动：条件步骤 / 中断取消 / 人工接管。

这三件事是赛题第 48 行点名要的，也是最容易被做成「演示动画」的地方。
所以这里的用例全部对着**真实副作用**断言，不看过场文案：
  条件不成立 → 钱必须一分没动；取消之后 → 那条待确认的授权必须真的失效。
"""

from __future__ import annotations

import re

from app.bank.sms import latest_code
from app.bank.db import connect, get_account
from app.engine.condition import evaluate
from app.engine.dag import DagError, validate
from app.engine.orchestrator import Orchestrator
from app.schemas import Plan, Step, StepCondition

USER = "u_1001"


def _balance(uid: str = USER) -> int:
    with connect() as conn:
        return int(get_account(conn, uid, "current")["balance_cents"])


def _mfa_code(text: str | None = None) -> str | None:
    """动态码从「手机」那一侧取（app/bank/sms.py），不从聊天回复里抠。

    ★ 参数保留只是为了少改调用点；它**必须被忽略**——如果哪天有人真拿 text 去匹配，
      就等于把「验证码不许回显」这条设计又拆了。
    """
    return latest_code(USER)


# ------------------------------------------------------------------ 条件求值

def test_condition_true_and_false():
    results = {"s0": {"balance_cents": 500}}
    assert evaluate(StepCondition(step="s0", field="balance_cents", op=">=", value=500), results)[0]
    assert not evaluate(
        StepCondition(step="s0", field="balance_cents", op=">", value=500), results
    )[0]


def test_condition_missing_step_or_field_is_false_not_true():
    """★ fail-closed：拿不准就当不成立，不能当成「应该是真的吧」。"""
    cond = StepCondition(step="s9", field="balance_cents", op=">", value=0)
    assert evaluate(cond, {})[0] is False
    cond2 = StepCondition(step="s0", field="nope", op=">", value=0)
    assert evaluate(cond2, {"s0": {"balance_cents": 1}})[0] is False


def test_condition_type_mismatch_is_false_not_crash():
    cond = StepCondition(step="s0", field="name", op=">", value=1)
    assert evaluate(cond, {"s0": {"name": "王强"}})[0] is False


def test_dag_rejects_condition_on_unknown_step():
    plan = Plan(steps=[
        Step(id="s1", tool="query_balance", args={},
             when=StepCondition(step="s0", field="balance_cents", op=">", value=0)),
    ])
    try:
        validate(plan)
    except DagError as exc:
        assert "不存在的步骤" in str(exc)
    else:
        raise AssertionError("条件引用了不存在的步骤，却通过了 DAG 校验")


def test_dag_autoadds_condition_dependency():
    """条件引用的步骤必须排在前面，靠自动补依赖保证，不靠调用方自觉。"""
    plan = Plan(steps=[
        Step(id="s0", tool="query_balance", args={}),
        Step(id="s1", tool="query_balance", args={},
             when=StepCondition(step="s0", field="balance_cents", op=">", value=0)),
    ])
    ordered = validate(plan)
    assert "s0" in ordered.get("s1").depends_on


# ------------------------------------------------------------------ 端到端

def test_conditional_transfer_executes_when_balance_enough():
    before = _balance()
    orch = Orchestrator()
    r = orch.handle_message(USER, "t_cond", "余额够的话给王强转100")
    assert [e["step"]["tool"] for e in r.executed][0] == "query_balance"
    assert r.pending, "余额够却没有进入转账确认"

    r2 = orch.handle_message(USER, "t_cond", _mfa_code(r.text))
    assert r2.executed and r2.executed[-1]["result"]["ok"]
    assert _balance() == before - 10_000


def test_conditional_transfer_skips_when_balance_short():
    """★ 余额不够时，一分钱不能动，也不该弹人脸/验证码。"""
    before = _balance()
    r = Orchestrator().handle_message(USER, "t_cond2", "余额够的话给王强转100000")
    # 查余额本身会执行（不查就判不了条件），但转账不能执行、也不能弹验证
    assert [e["step"]["tool"] for e in r.executed] == ["query_balance"]
    assert not r.pending
    assert r.skipped and r.skipped[0]["step"]["tool"] == "transfer"
    assert _balance() == before


# ------------------------------------------------------------------ 中断取消

def test_cancel_voids_pending_authorization_for_real():
    """★ 用户说「算了」之后，那条待确认的授权必须真的失效——不是嘴上说说。"""
    orch = Orchestrator()
    r = orch.handle_message(USER, "t_cancel", "转500给王强")
    assert r.pending

    r2 = orch.handle_message(USER, "t_cancel", "算了")
    assert "已取消" in r2.text

    # 之后再说「确认」不能把那笔转账执行掉
    before = _balance()
    r3 = orch.handle_message(USER, "t_cancel", "确认")
    assert not r3.executed
    assert _balance() == before


def test_cancel_phrase_does_not_swallow_a_real_request():
    """★「取消爱奇艺会员」开头也是「取消」，不能被当中断吞掉。"""
    orch = Orchestrator()
    r = orch.handle_message(USER, "t_cancel2", "转500给王强")
    assert r.pending
    r2 = orch.handle_message(USER, "t_cancel2", "取消爱奇艺会员")
    # 不是「已取消」回执，而是一次正常的退订请求
    assert "已取消，" not in r2.text
    assert r2.pending and r2.pending[0]["step_id"] == "s1"


# ------------------------------------------------------------------ 人工接管

def test_handoff_opens_ticket_with_audit_chain():
    orch = Orchestrator()
    orch.handle_message(USER, "t_handoff", "查余额")
    r = orch.handle_message(USER, "t_handoff", "转人工")
    assert "工单号 ho_" in r.text
    assert "session t_handoff" in r.text
    code = re.search(r"共 (\d+) 条记录", r.text)
    assert code and int(code.group(1)) >= 2, "工单没带上审计链"


def test_handoff_also_voids_pending_authorization():
    orch = Orchestrator()
    r = orch.handle_message(USER, "t_handoff2", "转500给王强")
    assert r.pending
    orch.handle_message(USER, "t_handoff2", "转人工")
    before = _balance()
    assert not orch.handle_message(USER, "t_handoff2", "确认").executed
    assert _balance() == before
