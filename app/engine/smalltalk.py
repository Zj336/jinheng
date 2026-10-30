"""寒暄、能力介绍、致谢 —— 全部确定性回答，不经过任何模型。

★ 为什么这类问题必须绕开 LLM：
  「你能干什么」是整个系统里唯一一个「模型的输出会被用户当成事实」的问题。
  模型如果说它支持「跨行汇款」，而代码里根本没有这个工具，用户一试就穿帮；
  更糟的是，这一穿帮恰好发生在用户问的第一个问题上。
  所以答案从工具注册表 `REGISTRY` 现场生成——能力清单与真实能力同源，不可能漂移。

★ 为什么它放在编排层而不是 LLM 适配层：
  这不是「理解」问题，是「陈述事实」。让它走 LLM，就等于把陈述事实的机会
  交给一个可能编造的东西；而放在编排层的前置分支，它连模型都碰不到。
"""

from __future__ import annotations

import re

# 问候：整句就是一个招呼，才算。★ 不放松成「包含」，否则
# 「你好，帮我转500给王强」会被当成寒暄吞掉，转账根本没发生。
_GREETING_RE = re.compile(
    r"^\s*(你好|您好|hi|hello|hey|嗨|哈喽|在吗|在不在|有人吗|早上好|上午好|中午好|下午好|晚上好)\s*[!！。.~～]*\s*$",
    re.IGNORECASE,
)

# 能力询问
_CAPABILITY_RE = re.compile(
    r"(你能(帮我)?(干|做|办)|你会(干|做)?(些)?什么|你有什么(功能|能力|用)|有什么功能|"
    r"能做什么|能干什么|可以做什么|支持(哪些|什么)|帮助|help|怎么用|如何使用|使用说明|"
    r"介绍一下你|你是谁|你是什么)",
    re.IGNORECASE,
)

_THANKS_RE = re.compile(r"^\s*(谢谢|多谢|感谢|thanks|thank you|thx)\s*[!！。.]*\s*$", re.IGNORECASE)
_BYE_RE = re.compile(r"^\s*(再见|拜拜|bye|goodbye|晚安)\s*[!！。.]*\s*$", re.IGNORECASE)

_LEVEL_ICON = {"green": "🟢 直接查（不改状态、不动钱）",
               "yellow": "🟡 需要你点一下确认",
               "red": "🔴 需要 6 位动态验证码"}

# 每个示例都必须是测试里真跑通过的句子——示例说了做不到，比不说更糟
EXAMPLES: list[tuple[str, str]] = [
    ("查一下余额", "纯查询，绿色直通"),
    ("余额够的话给王强转500", "条件步骤：先查余额，够了才转"),
    ("我有哪些卡", "卡片列表"),
    ("挂失尾号2233的信用卡", "红色，要动态码"),
    ("申购1000元稳健增利90天", "理财申购，红色"),
    ("分析下近30天的消费结构", "模型写分析代码 → 沙箱执行"),
    ("我有哪些订阅", "订阅清单"),
    ("取消爱奇艺订阅", "黄色，需确认"),
    ("算了", "中断：把挂着的授权全部作废"),
    ("转人工", "人工接管：带上整段会话的审计链开工单"),
]


def _level_lines() -> str:
    """按风险等级罗列工具。现场读注册表，不写死。"""
    try:
        from app.tools.base import REGISTRY
    except Exception:  # noqa: BLE001  注册表不可用时宁可不列，也不列错
        return ""
    buckets: dict[str, list[str]] = {"green": [], "yellow": [], "red": []}
    for meta in REGISTRY.values():
        bucket = buckets.setdefault(meta.base_level.value, [])
        if meta.label not in bucket:
            bucket.append(meta.label)
    lines = []
    for level in ("green", "yellow", "red"):
        if buckets.get(level):
            lines.append(f"{_LEVEL_ICON[level]}：{'、'.join(buckets[level])}")
    return "\n".join(lines)


def _capability_text() -> str:
    head = (
        "我是金衡 Statera，一个「可审计」的银行智能体。"
        "大模型只负责听懂和排计划，能不能做、要不要验证、钱怎么动，由一套"
        "**从不读自然语言**的规则内核裁决。\n\n"
        "下面这些是真实注册在册的能力（不是宣传语，清单直接读代码里的工具注册表）：\n"
    )
    body = _level_lines()
    tail = "\n\n可以直接这样说：\n" + "\n".join(
        f"  · {say}    —— {why}" for say, why in EXAMPLES
    )
    return head + (body + tail if body else tail)


def answer(text: str) -> str | None:
    """命中寒暄/能力/致谢返回一句回答；不命中返回 None，交回正常流程。"""
    if _CAPABILITY_RE.search(text):
        return _capability_text()
    if _GREETING_RE.match(text):
        return (
            "你好，我是金衡 Statera —— 银行的 AI 智能体，权限分级是绿/黄/红三色，"
            "所有动钱的动作都要你确认或验证码，且全程留审计。\n"
            "想先看看我能做什么，直接说「你能帮我干什么」；想直接试，说「查一下余额」。"
        )
    if _THANKS_RE.match(text):
        return "不客气。还有需要随时说——涉及资金的动作我都会先跟你确认再执行。"
    if _BYE_RE.match(text):
        return "再见。本次会话的每一步决策都已经写进审计链，随时可以回查。"
    return None
