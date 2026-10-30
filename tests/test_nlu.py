"""口语理解：把「一次听懂率」当成一条可回归的指标来盯。

起因是 2026-10-30 拿 12 句「评审真的会这么问」的口语去打，一次听懂只有 5/12，
而且其中一句是**自信地答错**：

    「我上个月点外卖花了多少钱」 → 「您的活期账户余额为 ¥24,680.50」

余额分支只看「多少钱」三个字，把「花了多少钱」也吃了进去。
答错比听不懂严重得多——听不懂用户会换个说法，答错用户会认为系统在胡乱应付。

下面每条用例都是当时真跑出来的原句，不许改写成更规整的书面语。
"""

from __future__ import annotations

from app.engine.orchestrator import Orchestrator

USER = "u_1001"


def _say(session: str, text: str):
    return Orchestrator().handle_message(USER, session, text)


def test_spending_question_is_not_answered_with_balance():
    """★ 「花了多少钱」≠「有多少钱」。这是最贵的一类错：静默答错。"""
    r = _say("nlu1", "我上个月点外卖花了多少钱")
    assert "余额" not in r.text
    assert any(e["step"]["tool"] in ("query_transactions", "analyze_spending") for e in r.executed)


def test_overspend_question_goes_to_analysis():
    r = _say("nlu2", "这个月是不是超支了")
    assert "接不住" not in r.text
    assert any(e["step"]["tool"] == "analyze_spending" for e in r.executed)


def test_unexpected_charge_question_goes_to_analysis():
    r = _say("nlu3", "帮我看看有没有乱扣费")
    assert any(e["step"]["tool"] == "analyze_spending" for e in r.executed)


def test_colloquial_unsubscribe_is_understood():
    """「把爱奇艺退了吧」里没有「订阅/会员/续费」任何一个词。"""
    r = _say("nlu4", "把爱奇艺退了吧")
    assert "接不住" not in r.text
    assert r.pending, "口语退订应当进入确认，而不是被当成听不懂"


def test_pronoun_recipient_asks_instead_of_denying():
    """★「再给他转100」里的「他」是待解析的指代，不是收款人名字。

    拿去匹配的结果是 P-011「未找到收款人「他」」——一个**拒绝**。
    用户需要的是被追问，不是被驳回。
    """
    r = _say("nlu5", "再给他转100")
    assert not r.denied, "指代不清不该走到拒绝"
    assert "还差" in r.text or "指的是谁" in r.text


def test_clarifying_turn_says_so_in_its_result():
    """追问这件事要由引擎说清楚，不能让调用方去文案里猜。

    mock 生成的追问带「还差…请补充」，真模型生成的不带——只按关键字判断，
    同一件事在对照表里会被记成两种结果（真模型的追问被记成「其他(说明类)」）。
    """
    r = _say("nlu9", "再给他转100")
    assert r.clarify, "该轮是追问，clarify 不该为空"
    assert not r.executed and not r.pending


def test_a_completed_turn_is_not_marked_as_clarifying():
    r = _say("nlu10", "查一下余额")
    assert r.clarify == [], "办完的轮次不该被标成追问"


def test_balance_question_still_works():
    """修 bug 不能把原来对的也修坏。"""
    for s, q in (("nlu6", "查一下余额"), ("nlu7", "我还有多少钱"), ("nlu8", "卡里有多少钱")):
        r = _say(s, q)
        assert any(e["step"]["tool"] == "query_balance" for e in r.executed), q
