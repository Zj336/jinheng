"""两个「看起来正常、其实在悄悄吃用户输入」的 bug 的回归测试。

两个 bug 都是真人实跑时撞出来的，共同点是：不报错、不崩溃、日志里也没有异常，
只是把用户的正常操作换成了另一个结果。这类缺陷靠代码评审很难发现，
只有把「当时那句话」原样写进测试才守得住。

  1. 动态码用 `\\d{6}` 匹配 → 「给王强转500000」里的 500000 被判成验证码，
     一句完全无关的转账请求变成「验证码不正确」。
  2. 跨多轮悬着的授权不会因为用户改口而失效 → 用户确认时可能确认到十分钟前那一笔。
"""

from __future__ import annotations

import re

from app.bank.db import connect, get_account
from app.bank.sms import latest_code
from app.engine.orchestrator import Orchestrator

USER = "u_1001"


def _mfa_code(text: str | None = None) -> str:
    """动态码从「手机」侧取，不从聊天回复里抠（参数保留只为少改调用点，必须忽略）。"""
    code = latest_code(USER)
    assert code, "短信通道里没有取到动态码"
    return code


def _pending_rows(session_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, kind, status FROM confirmations WHERE session_id=? ORDER BY created_at",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _balance(uid: str = USER) -> int:
    with connect() as conn:
        return int(get_account(conn, uid, "current")["balance_cents"])


# ------------------------------------------------------------------ 动态码粒度

def test_six_digit_amount_is_not_swallowed_as_mfa_code():
    """★ 真实的原句：等待理财申购验证码时说了「余额够的话给王强转500000」。

    500000 是六位数字，旧实现把它当验证码比对，于是：
    用户以为系统听不懂他的转账请求，系统以为用户输错了验证码。
    """
    orch = Orchestrator()
    r = orch.handle_message(USER, "t_code1", "申购1000元稳健增利90天")
    assert r.pending, "理财申购应当进入等待验证码"

    r2 = orch.handle_message(USER, "t_code1", "余额够的话给王强转500000")

    assert "验证码不正确" not in r2.text, (
        "句子里的金额被当成了验证码——这正是要修的 bug"
    )
    # 条件转账应当被正常评估：余额不够 → 跳过，而不是「验证码错误」
    assert any(s["step"]["tool"] == "transfer" for s in r2.skipped)


def test_code_with_prefix_is_accepted():
    """「验证码 123456」是人之常情，必须认；但要比对 group(1)，不是整句。"""
    orch = Orchestrator()
    r = orch.handle_message(USER, "t_code2", "申购1000元稳健增利90天")
    code = _mfa_code(r.text)

    r2 = orch.handle_message(USER, "t_code2", f"验证码{code}")
    assert "验证码不正确" not in r2.text
    assert r2.executed and r2.executed[-1]["step"]["tool"] == "purchase_wealth"
    assert r2.executed[-1]["result"]["ok"]


def test_code_embedded_in_a_sentence_does_not_burn_an_attempt():
    """带金额的转账句子不能被记成「输错一次」——那会白白逼近作废上限。"""
    orch = Orchestrator()
    r = orch.handle_message(USER, "t_code3", "申购1000元稳健增利90天")
    _mfa_code(r.text)
    cf_id = _pending_rows("t_code3")[-1]["id"]

    r2 = orch.handle_message(USER, "t_code3", "给王强转500000")
    assert "验证码不正确" not in r2.text

    row = [x for x in _pending_rows("t_code3") if x["id"] == cf_id][0]
    # 不是「输错被记了一次」（那会留在 pending 上，attempts+1），
    # 而是「用户已经改口说别的事了」→ 被顶掉。两者审计上必须能分清。
    assert row["status"] == "superseded"


def test_talking_about_the_code_gets_a_concrete_hint():
    """用户照着说明打了句「把验证码填进去」——该给具体提示，不是一段通用帮助。"""
    orch = Orchestrator()
    r = orch.handle_message(USER, "t_code4", "申购1000元稳健增利90天")
    r2 = orch.handle_message(USER, "t_code4", "把验证码填进去")
    assert "6 位动态码" in r2.text
    assert "我没能理解" not in r2.text


# ------------------------------------------------------------------ 授权生命周期

def test_new_request_supersedes_previous_authorization():
    """★ 用户改口做别的事之后，之前那条授权必须失效，绝不能留着还能被确认。

    用「取消订阅」而不是转账来做这个用例：它是真正的黄色确认级，
    如果 supersede 失效，「确认」会把那条订阅真的退掉——测试才有杀伤力。
    """
    orch = Orchestrator()
    r = orch.handle_message(USER, "t_sup", "取消爱奇艺订阅")
    assert r.pending

    orch.handle_message(USER, "t_sup", "查一下余额")

    rows = _pending_rows("t_sup")
    assert rows and rows[0]["status"] == "superseded", rows

    r3 = orch.handle_message(USER, "t_sup", "确认")
    assert not r3.executed, "旧授权已被顶掉，不该还能被确认执行"


def test_supersede_does_not_touch_approved_rows():
    """已完成的授权是历史事实，不能被后续请求改写状态（审计链要经得起回查）。"""
    orch = Orchestrator()
    orch.handle_message(USER, "t_sup2", "取消爱奇艺订阅")
    rows = _pending_rows("t_sup2")
    assert rows and rows[0]["kind"] == "confirm"
    orch.handle_message(USER, "t_sup2", "确认")
    orch.handle_message(USER, "t_sup2", "查一下余额")
    statuses = [r["status"] for r in _pending_rows("t_sup2")]
    assert "approved" in statuses
    assert "pending" not in statuses


# ------------------------------------------------------------------ 并发

def test_concurrent_confirms_execute_only_once():
    """★ 评审一定会问：「用户手抖点两下，会不会开两张卡 / 转两笔？」

    当前结论是不会。**但要说清楚为什么不会**：不是因为代码里有原子抢占，
    而是因为 sqlite 的写锁 + 「一轮对话一个事务」把整轮串行化了——
    这是个副作用，不是承诺。换 Postgres、或把第一句写操作挪走，它就会失效。
    所以用这条用例把这个性质钉住：将来换存储时，它会先变红提醒我们。
    """
    import threading

    from app.bank.db import list_cards

    def n_cards() -> int:
        with connect() as c:
            return len(list_cards(c, USER))

    orch = Orchestrator()
    assert orch.handle_message(USER, "t_race", "申请一张虚拟卡").pending
    before = n_cards()

    n = 8
    barrier = threading.Barrier(n)
    results: list = []

    def fire() -> None:
        barrier.wait()
        try:
            results.append(orch.handle_message(USER, "t_race", "确认"))
        except Exception as exc:  # noqa: BLE001
            results.append(exc)

    threads = [threading.Thread(target=fire) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    executed = [r for r in results if not isinstance(r, Exception) and r.executed]
    assert not [r for r in results if isinstance(r, Exception)], "并发确认不应抛异常"
    assert n_cards() == before + 1, "同一张授权被并发确认了多次（重复开卡）"
    assert len(executed) == 1


# ------------------------------------------------------------------ 寒暄 / 能力

def test_capability_question_is_answered_from_the_registry():
    """能力介绍不能是宣传语：它必须从工具注册表现场生成，说了就一定有。"""
    from app.tools.base import REGISTRY

    r = Orchestrator().handle_message(USER, "t_meta", "你能帮我干什么")
    assert "金衡 Statera" in r.text
    assert "我没能理解" not in r.text
    # 至少有一半工具的 label 出现在回答里——手工维护的清单做不到这一点
    labels = [m.label for m in REGISTRY.values()]
    hit = sum(1 for lb in labels if lb in r.text)
    assert hit >= len(labels) // 2, f"能力清单与注册表对不上：命中 {hit}/{len(labels)}"


def test_greeting_is_short_and_does_not_hijack_real_requests():
    orch = Orchestrator()
    r = orch.handle_message(USER, "t_meta2", "你好")
    assert "金衡" in r.text

    # ★ 关键：带招呼语的真实请求不能被当成寒暄吞掉
    r2 = orch.handle_message(USER, "t_meta3", "你好，帮我转500给王强")
    assert "金衡 Statera，一个" not in r2.text
    assert r2.pending, "「你好，帮我转…」应当进入转账确认，而不是被当成寒暄"
