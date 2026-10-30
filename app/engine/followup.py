"""槽位续接：用户补一句话，把上一轮没问完的意图补全。

★ 起因（2026-10-30 用户实跑）：
      系统：还差要操作的卡片（尾号后四位或卡种）没告诉我，请补充。
      用户：2233
      系统：「2233」这句我接不住……

  系统刚问完，转头就听不懂自己问的问题——这比「第一句没听懂」难看得多。

★ 做法：编排层把「上一轮缺哪些槽位」记在会话里（一轮即失效），
  下一轮先用**同一套提取函数**去补槽，补全了就拼回一句完整的话再走正常流水线。
  为什么拼回一句话、而不是直接造一个 Intent 出来：
  规划逻辑（条件步骤、步骤依赖、步骤 id）只有一份，在 `llm/mock.py` 里。
  另起一份「补槽后怎么规划」就等于把同一件事写两遍，两份迟早会说的不一样。
"""

from __future__ import annotations

import re

from app.llm import mock

_CARD_CN = {"virtual": "虚拟卡", "credit": "信用卡", "debit": "借记卡"}


def _amount_text(cents: int) -> str:
    return f"{cents // 100}" if cents % 100 == 0 else f"{cents / 100:.2f}"


def _bare_name(text: str) -> str | None:
    """「王强」这种只有名字、没有任何动词和数字的短句。

    加长度上限与字符限制是怕把「明天再说吧」当成收款人。
    """
    t = text.strip()
    if not (2 <= len(t) <= 8):
        return None
    if re.search(r"[\d，,。；;？?！!]", t):
        return None
    if re.search(r"(转|查|看|帮|吗|呢|吧|的|给|要|想|不)", t):
        return None
    return t


def _fill(intent: str, slot: str, text: str):
    if slot == "card":
        return mock._extract_card_keyword(text)
    if slot == "amount_cents":
        v = mock._extract_amount_cents(text)
        if v is not None:
            return v
        m = re.fullmatch(r"[¥￥]?\s*(\d+(?:\.\d{1,2})?)\s*(?:元|块|块钱)?", text.strip())
        if m:
            return int(float(m.group(1)) * 100)
        return None
    if slot == "to":
        return mock._extract_recipient(text) or _bare_name(text)
    if slot == "merchant":
        return mock._extract_sub_merchant(text) if len(text.strip()) <= 16 else None
    if slot == "product":
        return mock._extract_product_keyword(text) if len(text.strip()) <= 16 else None
    return None


def _card_phrase(card: str) -> str:
    if str(card).isdigit():
        return f"尾号{card}的卡"
    return _CARD_CN.get(str(card), str(card))


def _render_utterance(intent: str, slots: dict) -> str | None:
    try:
        if intent == "freeze_card":
            return f"挂失{_card_phrase(slots['card'])}"
        if intent == "unfreeze_card":
            return f"解挂{_card_phrase(slots['card'])}"
        if intent == "transfer":
            return f"转{_amount_text(slots['amount_cents'])}给{slots['to']}"
        if intent == "cancel_subscription":
            return f"取消{slots['merchant']}订阅"
        if intent == "purchase_wealth":
            return f"申购{_amount_text(slots['amount_cents'])}元{slots['product']}"
    except KeyError:
        return None
    return None


def complete(stored: list[dict] | None, text: str) -> str | None:
    """补齐后返回一句话；补不齐返回 None（交给正常解析，该追问就追问）。"""
    if not stored:
        return None
    text = (text or "").strip()
    if not text:
        return None

    parts: list[str] = []
    for item in stored:
        name = item.get("name") or ""
        slots = dict(item.get("slots") or {})
        missing = list(item.get("missing") or [])
        for slot in missing:
            value = _fill(name, slot, text)
            if value is None:
                return None
            slots[slot] = value
        utt = _render_utterance(name, slots)
        if utt is None:
            return None
        parts.append(utt)
    return "然后".join(parts) if parts else None
