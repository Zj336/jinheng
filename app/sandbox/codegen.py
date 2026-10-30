"""分析代码生成。

两种生成器，接口相同，产物都是「一段只读 data、给 result 赋值的 Python」：

  * template —— 确定性模板。零 API Key、零网络也能跑，是对照基线。
  * llm      —— 真·大模型生成，需要 JH_LLM_MODE=openai + Key。

★ 无论哪种产物，都必须穿过 astguard + 子进程沙箱才能执行。
  也就是说「模型生成的代码是不可信的输入」——这是本模块的安全前提，
  而不是「因为我们相信模型」。
"""

from __future__ import annotations

import re

from app.config import settings

Goal = str

_TEMPLATES: dict[str, str] = {
    # ---------------------------------------------------------- 分类结构
    "category_breakdown": '''
out = [i for i in data["items"] if i["direction"] == "out"]
total = sum(i["amount_cents"] for i in out)
by = {}
cnt = {}
mer = {}
for i in out:
    c = i.get("category") or "其他"
    by[c] = by.get(c, 0) + i["amount_cents"]
    cnt[c] = cnt.get(c, 0) + 1
    name = i.get("counterparty") or "未知商户"
    k = c + "" + name
    mer[k] = mer.get(k, 0) + i["amount_cents"]
rows = []
for c in by:
    # 「其他」占 47% 却不说是哪几笔，等于没分析。每一类都带上前三笔，
    # 大类可以下钻，小类也不占地方。
    pairs = []
    for k in mer:
        cat, name = k.split("")
        if cat == c:
            pairs.append((name, mer[k]))
    pairs.sort(key=lambda t: -t[1])
    rows.append({
        "category": c,
        "cents": by[c],
        "count": cnt[c],
        "pct_bp": (by[c] * 10000 // total) if total else 0,
        "top": [{"merchant": n, "cents": v} for n, v in pairs[:3]],
    })
rows.sort(key=lambda r: -r["cents"])
result = {"total_cents": total, "count": len(out), "rows": rows}
''',
    # ---------------------------------------------------------- Top 商户
    "top_merchants": '''
out = [i for i in data["items"] if i["direction"] == "out"]
total = sum(i["amount_cents"] for i in out)
by = {}
for i in out:
    k = i.get("counterparty") or "未知商户"
    by[k] = by.get(k, 0) + i["amount_cents"]
rows = []
for k in by:
    rows.append({"merchant": k, "cents": by[k],
                 "pct_bp": (by[k] * 10000 // total) if total else 0})
rows.sort(key=lambda r: -r["cents"])
result = {"total_cents": total, "count": len(out), "rows": rows[:5]}
''',
    # ---------------------------------------------------------- 异常排查
    "anomalies": '''
out = [i for i in data["items"] if i["direction"] == "out"]
res = []
for i in out:
    hour = int(i["ts"][11:13])
    cp = i.get("counterparty") or ""
    why = []
    if hour < 6:
        why.append("深夜交易（0-6 点）")
    if i["amount_cents"] >= 100000:
        why.append("单笔大额（≥1000 元）")
    if "未知" in cp or "境外" in cp:
        why.append("陌生或境外商户")
    if why:
        res.append({"ts": i["ts"], "counterparty": cp,
                    "cents": i["amount_cents"], "reasons": why})
res.sort(key=lambda r: -r["cents"])
result = {
    "count": len(res),
    "rows": res,
    "flagged_cents": sum(r["cents"] for r in res),
    "scanned": len(out),
}
''',
    # ---------------------------------------------------------- 周度趋势
    "trend": '''
out = [i for i in data["items"] if i["direction"] == "out"]
by_week = {}
for i in out:
    d = i["ts"][:10]
    month = int(d[5:7])
    week = (int(d[8:10]) - 1) // 7 + 1
    key = "%02d 月第 %d 周" % (month, week)
    by_week[key] = by_week.get(key, 0) + i["amount_cents"]
rows = []
for k in by_week:
    rows.append({"week": k, "cents": by_week[k]})
rows.sort(key=lambda r: r["week"])
result = {"rows": rows, "total_cents": sum(r["cents"] for r in rows)}
''',
}

_RENDER_HINT = {
    "category_breakdown": "按消费类目拆解近 N 天的支出结构与占比",
    "top_merchants": "找出近 N 天花钱最多的 5 个商户",
    "anomalies": "标记近 N 天的异常交易（深夜 / 大额 / 陌生或境外商户）",
    "trend": "按周统计近 N 天的支出走势",
}


def template_code(goal: str) -> str:
    if goal not in _TEMPLATES:
        raise KeyError(f"未知分析目标：{goal}")
    return _TEMPLATES[goal].strip() + "\n"


_LLM_SYS = """你是数据分析代码生成器。根据要求输出**一段 Python 代码**，不要任何解释、不要 markdown 代码块围栏。

硬性约束：
- 变量 data 已存在，是 dict：{"days": int, "items": [ {"ts": "YYYY-MM-DDTHH:MM:SS", "direction": "in|out", "amount_cents": int, "counterparty": str, "category": str, "memo": str} ]}
- 必须给变量 result 赋值一个 dict，只含可 JSON 序列化的类型（dict/list/str/int/float/bool/None）
- 金额单位一律是「分」（整数），所有金额运算必须用整数，禁止用浮点做钱的加减
- 禁止 import、禁止 open、禁止 eval/exec、禁止访问任何双下划线属性
- 只用内建函数：len/sum/min/max/sorted/range/int/str/dict/list/set/float/round
"""


def llm_code(goal: str, days: int) -> str | None:
    """真实 LLM 生成代码。未配置 Key 或调用失败时返回 None，由调用方回落到模板。"""
    if settings.llm_mode == "mock" or not settings.llm_api_key:
        return None

    import httpx

    user = (
        f"分析目标：{_RENDER_HINT.get(goal, goal)}\n"
        f"时间窗口：近 {days} 天\n"
        f"请只输出代码。"
    )
    try:
        resp = httpx.post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            json={
                "model": settings.llm_model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": _LLM_SYS},
                    {"role": "user", "content": user},
                ],
            },
            timeout=settings.llm_timeout_s,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"]
    except Exception:
        return None

    # 模型经常不自觉加围栏，剥掉
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", (text or "").strip())
    return text or None


def generate(goal: str, days: int) -> tuple[str, str]:
    """返回 (代码, 生成器名)。生成器名会写进审计日志，便于复现。"""
    code = llm_code(goal, days)
    if code:
        return code, "llm"
    return template_code(goal), "template"


def preview(code: str, limit: int = 400) -> str:
    """给前端/演示用的代码摘要。"""
    one = code.strip()
    return one if len(one) <= limit else one[:limit] + " …"


__all__ = ["Goal", "generate", "llm_code", "preview", "template_code"]
