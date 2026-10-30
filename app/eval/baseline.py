"""对照组：把权限规则写进 Prompt 的典型实现。

════════════════════════════════════════════════════════════════
这不是稻草人。这是绝大多数 Agent 项目真实的写法：
system prompt 里写着「大额转账需用户确认」，然后模型在运行时
从**同一段文本**里读取用户诉求、也读取用户声明。

攻击之所以能成，根本原因只有一个：
    防御手段（权限规则）和攻击目标（用户文本）走的是同一个通道。
用户只要在文本里说「我已授权」，模型就会把这句话当成事实。
════════════════════════════════════════════════════════════════

除此之外，它还复刻了三个常见疏漏：
  * 额度按单笔判，不维护日累计  →  拆单可绕过
  * 收款人按姓名全局查找，不校验归属 →  可跨用户
  * 「已确认」只看文本字样，不看凭证 →  会话可伪造
"""

from __future__ import annotations

import re
import sqlite3

from app.bank.db import connect, get_account, new_id, now_iso
from app.llm.base import Proposal
from app.llm.mock import MockLLM
from app.engine import guard

# Prompt 里常见的「已授权」判断关键词
_GRANTED_RE = re.compile(r"(已授权|已确认|已批准|管理员|风控|内部|特批|我同意|通过验证)")

SINGLE_LIMIT_CENTS = 100_000  # 只看单笔，1000 元


class NaiveResult:
    def __init__(self, text: str, executed: list[dict]) -> None:
        self.text = text
        self.executed = executed


class NaiveBaseline:
    name = "baseline"

    def __init__(self) -> None:
        self.llm = MockLLM()

    # 只看姓名，不校验归属——真实项目里最常见的写法
    @staticmethod
    def _find_any_contact(conn: sqlite3.Connection, keyword: str):
        kw = f"%{keyword}%"
        return conn.execute(
            "SELECT * FROM contacts WHERE name LIKE ? OR aliases LIKE ? LIMIT 1", (kw, kw)
        ).fetchone()

    def handle(self, user_id: str, session_id: str | None, message: str) -> NaiveResult:
        env = guard.inspect(message)
        proposal: Proposal = self.llm.propose(env)
        executed: list[dict] = []
        texts: list[str] = []

        with connect() as conn:
            for step in proposal.plan.steps:
                if step.tool != "transfer":
                    continue
                amount = int(step.args.get("amount_cents", 0))
                to = str(step.args.get("to", ""))

                # ★ 权限判定：读文本
                granted = bool(_GRANTED_RE.search(message))
                needs_check = amount > SINGLE_LIMIT_CENTS  # 只比单笔

                if needs_check and not granted:
                    texts.append("这笔金额较大，请确认后再操作。")
                    continue

                contact = self._find_any_contact(conn, to)
                if contact is None:
                    texts.append(f"未找到收款人「{to}」。")
                    continue

                acc = get_account(conn, user_id, "current")
                if acc is None or amount > acc["balance_cents"]:
                    texts.append("余额不足。")
                    continue

                # 直接执行——没有确认、没有凭证、没有累计、没有审计
                conn.execute(
                    "UPDATE accounts SET balance_cents = balance_cents - ? WHERE id=?",
                    (amount, acc["id"]),
                )
                conn.execute(
                    "INSERT INTO transactions(id,account_id,user_id,ts,direction,amount_cents,"
                    "counterparty,category,memo) VALUES (?,?,?,?,?,?,?,?,?)",
                    (new_id("t_"), acc["id"], user_id, now_iso(), "out", amount,
                     contact["name"], "转账", ""),
                )
                executed.append({"step": step.model_dump(), "amount_cents": amount,
                                 "to": contact["name"]})
                texts.append(f"已向 {contact['name']} 转账 {amount / 100:.2f} 元。")

        return NaiveResult(text="\n".join(texts) or "（未执行）", executed=executed)
