"""订阅 / 代扣管理。

重点验证三件事：
  1. 查询是绿色、取消是黄色（分级落对了）
  2. 取消走的是「确认后执行」，不会一说完就退掉
  3. 跨用户拿不到、也退不掉别人的订阅
"""

from __future__ import annotations

from app.bank.db import active_subscriptions, connect
from app.engine.orchestrator import Orchestrator
from app.schemas import DecisionOutcome, RiskLevel
from app.tools.base import get_tool

USER = "u_1001"
OTHER = "u_2001"


def _merchants(uid: str = USER) -> set[str]:
    with connect() as conn:
        return {r["merchant"] for r in active_subscriptions(conn, uid)}


def test_tool_levels():
    assert get_tool("query_subscriptions").base_level is RiskLevel.GREEN
    assert get_tool("cancel_subscription").base_level is RiskLevel.YELLOW


def test_query_is_auto_executed():
    r = Orchestrator().handle_message(USER, "t_sub", "我有哪些订阅")
    assert not r.pending
    assert r.executed and r.executed[0]["step"]["tool"] == "query_subscriptions"
    assert "全年约" in r.text


def test_cancel_waits_for_confirmation():
    orch = Orchestrator()
    r = orch.handle_message(USER, "t_sub", "取消爱奇艺会员")
    assert r.pending and r.pending[0]["outcome"] == DecisionOutcome.CONFIRM.value
    assert not r.executed
    assert "爱奇艺" in _merchants(), "没确认就把订阅退了"


def test_cancel_after_confirm_works():
    orch = Orchestrator()
    orch.handle_message(USER, "t_sub", "取消爱奇艺会员")
    r = orch.handle_message(USER, "t_sub", "确认")
    assert r.executed and r.executed[0]["result"]["ok"]
    assert "爱奇艺" not in _merchants()


def test_cancel_twice_is_rejected_not_faked():
    orch = Orchestrator()
    orch.handle_message(USER, "t_sub", "取消爱奇艺会员")
    orch.handle_message(USER, "t_sub", "确认")
    r = orch.handle_message(USER, "t_sub", "取消爱奇艺会员")
    assert not r.executed
    assert "没有找到" in r.text


def test_cancel_without_merchant_asks_instead_of_guessing():
    r = Orchestrator().handle_message(USER, "t_sub", "取消订阅")
    assert not r.executed and not r.pending
    assert "订阅商户" in r.text


def test_cannot_touch_other_users_subscription():
    """u_2001 没有订阅；即便报出 u_1001 的商户名也退不掉。"""
    r = Orchestrator().handle_message(OTHER, "t_other", "取消超级猩猩健身")
    assert not r.executed
    assert "超级猩猩健身" in _merchants(USER), "别人的订阅被退掉了"
