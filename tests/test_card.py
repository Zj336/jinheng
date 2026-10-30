"""卡片管理。

重点验证：
  1. 查询绿色、虚拟卡申请黄色、挂失/解挂红色（分级落对了）
  2. 挂失必须先过早验证，不能一句话就锁卡
  3. 「解除挂失」不会被理解成「再挂一次」（同义词顺序坑）
  4. 别人的卡碰不到
"""

from __future__ import annotations

import re

from app.bank.sms import latest_code
from app.bank.db import connect, list_cards
from app.engine.orchestrator import Orchestrator
from app.schemas import DecisionOutcome, RiskLevel
from app.tools.base import get_tool

USER = "u_1001"
OTHER = "u_2001"


def _status(last4: str, uid: str = USER) -> str | None:
    with connect() as conn:
        return next((c["status"] for c in list_cards(conn, uid) if c["last4"] == last4), None)


def _mfa_code(text: str | None = None) -> str | None:
    """动态码从「手机」那一侧取（app/bank/sms.py），不从聊天回复里抠。

    ★ 参数保留只是为了少改调用点；它**必须被忽略**——如果哪天有人真拿 text 去匹配，
      就等于把「验证码不许回显」这条设计又拆了。
    """
    return latest_code(USER)


def test_tool_levels():
    assert get_tool("query_cards").base_level is RiskLevel.GREEN
    assert get_tool("apply_virtual_card").base_level is RiskLevel.YELLOW
    assert get_tool("freeze_card").base_level is RiskLevel.RED
    assert get_tool("unfreeze_card").base_level is RiskLevel.RED


def test_query_is_auto_executed():
    r = Orchestrator().handle_message(USER, "t_card", "我有哪些卡")
    assert not r.pending
    assert r.executed and r.executed[0]["step"]["tool"] == "query_cards"
    assert "尾号" in r.text


def test_freeze_requires_mfa_and_does_not_execute_early():
    """★ 没做多因子之前，卡必须还是正常状态。"""
    orch = Orchestrator()
    r = orch.handle_message(USER, "t_freeze", "挂失尾号6699的信用卡")
    # 6699 不存在 → 应该被前置解析直接拒掉，而不是先要验证码
    assert not r.pending and not r.executed
    assert "没有找到" in r.text


def test_freeze_flow_end_to_end():
    orch = Orchestrator()
    r = orch.handle_message(USER, "t_freeze2", "挂失尾号2233的信用卡")
    assert r.pending and r.pending[0]["outcome"] == DecisionOutcome.MFA.value
    assert _status("2233") == "active", "还没验证就把卡挂了"

    code = _mfa_code(r.text)
    r2 = orch.handle_message(USER, "t_freeze2", code)
    assert r2.executed and r2.executed[0]["result"]["ok"]
    assert _status("2233") == "frozen"

    # 解开，别污染后续用例
    r3 = orch.handle_message(USER, "t_freeze2", "解除尾号2233的挂失")
    code = _mfa_code(r3.text)
    orch.handle_message(USER, "t_freeze2", code)
    assert _status("2233") == "active"


def test_unfreeze_is_parsed_as_unfreeze_not_freeze():
    """★ 「解除挂失」同时含「挂失」两字，同义词顺序写反会把卡又锁一遍。"""
    from app.llm.mock import MockLLM

    intent = MockLLM()._parse_segment("解除尾号2233的挂失")
    assert intent.name == "unfreeze_card"


def test_freeze_twice_is_rejected_by_precheck():
    orch = Orchestrator()
    r = orch.handle_message(USER, "t_freeze3", "挂失尾号2233的信用卡")
    orch.handle_message(USER, "t_freeze3", _mfa_code(r.text))
    r2 = orch.handle_message(USER, "t_freeze3", "挂失尾号2233的信用卡")
    assert not r2.pending and not r2.executed
    assert "已经处于挂失状态" in r2.text

    r3 = orch.handle_message(USER, "t_freeze3", "解除尾号2233的挂失")
    orch.handle_message(USER, "t_freeze3", _mfa_code(r3.text))


def test_apply_virtual_card_needs_confirmation():
    orch = Orchestrator()
    r = orch.handle_message(USER, "t_vcard", "申请一张虚拟卡")
    assert r.pending and r.pending[0]["outcome"] == DecisionOutcome.CONFIRM.value
    assert "¥1,000.00" in r.text, "确认提示必须显示真实限额，不能显示 0"

    r2 = orch.handle_message(USER, "t_vcard", "确认")
    assert r2.executed and r2.executed[0]["result"]["ok"]
    assert r2.executed[0]["result"]["data"]["last4"]


def test_cannot_freeze_other_users_card():
    r = Orchestrator().handle_message(OTHER, "t_other_card", "挂失尾号6688的借记卡")
    assert not r.executed
    assert _status("6688") == "active", "别人的卡被挂失了"
