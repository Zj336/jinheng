"""理财操作。

重点验证：
  1. 查询绿色、申购红色（且永远红色，不因金额小而降级）
  2. 申购必须过人脸/短信，且没验证前钱不能动
  3. ★ 申购**不占用**转账的当日额度——否则买完理财就转不出钱
  4. 起购金额不足 / 余额不足 在要验证码之前就被挡掉
"""

from __future__ import annotations

import re

from app.bank.sms import latest_code
from app.bank.db import connect, daily_transferred_cents, get_account, list_wealth_orders
from app.engine.orchestrator import Orchestrator
from app.schemas import DecisionOutcome, RiskLevel
from app.tools.base import get_tool

USER = "u_1001"


def _balance() -> int:
    with connect() as conn:
        return int(get_account(conn, USER, "current")["balance_cents"])


def _mfa_code(text: str | None = None) -> str | None:
    """动态码从「手机」那一侧取（app/bank/sms.py），不从聊天回复里抠。

    ★ 参数保留只是为了少改调用点；它**必须被忽略**——如果哪天有人真拿 text 去匹配，
      就等于把「验证码不许回显」这条设计又拆了。
    """
    return latest_code(USER)


def test_tool_levels():
    assert get_tool("query_wealth_products").base_level is RiskLevel.GREEN
    assert get_tool("purchase_wealth").base_level is RiskLevel.RED
    assert get_tool("purchase_wealth").is_fund_movement is True


def test_query_is_auto_executed():
    r = Orchestrator().handle_message(USER, "t_wealth", "在售理财有哪些")
    assert not r.pending
    assert r.executed and r.executed[0]["step"]["tool"] == "query_wealth_products"
    assert "业绩比较基准" in r.text


def test_purchase_needs_mfa_and_moves_no_money_before_that():
    before = _balance()
    orch = Orchestrator()
    r = orch.handle_message(USER, "t_buy", "申购1000元稳健增利90天")
    assert r.pending and r.pending[0]["outcome"] == DecisionOutcome.MFA.value
    assert _balance() == before, "还没验证就扣钱了"

    code = _mfa_code(r.text)
    r2 = orch.handle_message(USER, "t_buy", code)
    assert r2.executed and r2.executed[0]["result"]["ok"]
    assert _balance() == before - 100_000
    with connect() as conn:
        assert any(o["product_name"] == "稳健增利90天" for o in list_wealth_orders(conn, USER))


def test_purchase_does_not_consume_transfer_daily_limit():
    """★ 核心回归：理财申购若被记进转账台账，用户买完理财就转不了账了。"""
    with connect() as conn:
        before = daily_transferred_cents(conn, USER)
    orch = Orchestrator()
    r = orch.handle_message(USER, "t_buy2", "申购1000元稳健增利90天")
    orch.handle_message(USER, "t_buy2", _mfa_code(r.text))
    with connect() as conn:
        assert daily_transferred_cents(conn, USER) == before, "理财申购占用了转账当日额度"


def test_min_amount_is_checked_before_asking_for_mfa():
    r = Orchestrator().handle_message(USER, "t_buy3", "申购50元平衡优选180天")
    assert not r.pending and not r.executed
    assert "起购" in r.text


def test_insufficient_balance_is_checked_before_mfa():
    r = Orchestrator().handle_message(USER, "t_buy4", "申购200000元稳健增利90天")
    assert not r.pending and not r.executed
    assert "余额不足" in r.text


def test_unknown_product_is_rejected():
    r = Orchestrator().handle_message(USER, "t_buy5", "申购1000元火星挖矿基金")
    assert not r.pending and not r.executed
    assert "没有找到" in r.text
