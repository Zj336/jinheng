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


# ★ 输出契约必须逐字写进提示词。
#   真机联调第一轮就踩了这个坑：模型写出的代码是对的，但它把结果组织成
#   {"window_days":…, "breakdown":[…]},而表述层要读 {"rows":[…]}
#   —— 于是「近 30 天没有支出记录」被打印出来，**明明有 11 笔支出**。
#   没有报错、没有异常，只是安静地答错。这类错误比崩溃危险得多。
_CONTRACT = {
    "category_breakdown": """result 必须恰好是：
{"rows": [{"category": str, "cents": int, "count": int, "pct_bp": int,
           "top": [{"merchant": str, "cents": int}]}],
 "count": int, "total_cents": int}
- rows 按 cents 降序；pct_bp 是占比的万分比整数（91.55% → 9155）
- top 给前 3 个商户，只对类目名为「其他」或占比 ≥ 20% 的类目给（没有就给空列表）""",
    "top_merchants": """result 必须恰好是：
{"rows": [{"merchant": str, "cents": int, "pct_bp": int}], "total_cents": int}
- 最多 5 行，按 cents 降序""",
    "anomalies": """result 必须恰好是：
{"rows": [{"ts": str, "counterparty": str, "cents": int, "reasons": [str]}],
 "count": int, "flagged_cents": int, "scanned": int}
- reasons 用中文短语，只能是这四种，判定条件**逐字照此执行**，不要自行发明：
  「深夜交易」= 交易时间 0:00–5:59
  「大额」    = 单笔 ≥ 100000 分（1000 元）
  「陌生商户」= 商户名为空，或商户名里含「未知」二字（如「未知商户」）
  「境外商户」= 商户名里含「境外」二字（如「某境外商户」）
- 商户名里出现汉字、英文品牌名（瑞幸咖啡 / 美团外卖 / 爱奇艺）**都不算陌生商户**""",
    "trend": """result 必须恰好是：
{"rows": [{"week": str, "cents": int}], "total_cents": int}
- week 形如「10 月第 2 周」，按时间升序""",
}


_LLM_SYS = """你是数据分析代码生成器。根据要求输出**一段 Python 代码**，不要任何解释、不要 markdown 代码块围栏。

硬性约束：
- 变量 data 已存在，是 dict：{"days": int, "items": [ {"ts": "YYYY-MM-DDTHH:MM:SS", "direction": "in|out", "amount_cents": int, "counterparty": str, "category": str, "memo": str} ]}
- 必须给变量 result 赋值一个 dict，只含可 JSON 序列化的类型（dict/list/str/int/float/bool/None）
- 金额单位一律是「分」（整数），所有金额运算必须用整数，禁止用浮点做钱的加减
- 只统计 direction == "out" 的记录
- 禁止 import、禁止 open、禁止 eval/exec、禁止访问任何双下划线属性
- 只用内建函数：len/sum/min/max/sorted/range/int/str/dict/list/set/float/round
- **没有 datetime / time / re / json 模块可用**（沙箱不允许 import）。时间一律按字符串切片解析：
  `ts` 形如 "2026-10-20T14:00:00"，年 ts[0:4]、月 ts[5:7]、日 ts[8:10]、时 ts[11:13]
"""


def llm_code(goal: str, days: int) -> str | None:
    """真实 LLM 生成代码。未配置 Key 或调用失败时返回 None，由调用方回落到模板。"""
    if settings.llm_mode == "mock" or not settings.llm_api_key:
        return None

    import httpx

    user = (
        f"分析目标：{_RENDER_HINT.get(goal, goal)}\n"
        f"时间窗口：近 {days} 天\n\n"
        f"输出契约（对象字段名必须完全一致，表述层按它取值）：\n{_CONTRACT.get(goal, '')}\n\n"
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


def validate_result(goal: str, result, items: list[dict]) -> str | None:
    """检查生成代码的产出有没有按契约来。None = 合格，否则返回人话原因。

    ★ 为什么不只看「跑没跑起来」：代码跑通了、返回了一个结构不同的 dict，
      表述层读不到 `rows` 就会打印「近 30 天没有支出记录」——而账上明明有 11 笔。
      **安静地答错比直接报错危险得多**，所以这里有一道结构校验：
      模型可以自由地写代码，但产物必须能对上表述层的契约，对不上就回落模板。
    """
    if not isinstance(result, dict):
        return f"结果不是 JSON 对象（拿到 {type(result).__name__}）"

    out_items = [i for i in items if i.get("direction") == "out"]
    out_total = sum(int(i.get("amount_cents") or 0) for i in out_items)

    def need_list(key: str) -> list | None:
        value = result.get(key)
        return value if isinstance(value, list) else None

    def need_int(key: str) -> bool:
        return isinstance(result.get(key), int)

    if goal in ("category_breakdown", "top_merchants", "trend"):
        rows = need_list("rows")
        if rows is None:
            return "缺少 rows 列表（字段名必须是 rows）"
        if not need_int("total_cents"):
            return "缺少整数 total_cents"
        if out_items and not rows:
            return f"输入有 {len(out_items)} 笔支出，结果却是空的"
        if out_items and int(result.get("total_cents") or 0) <= 0:
            return "输入有支出，结果合计却为 0"
        key = {"category_breakdown": "category", "top_merchants": "merchant", "trend": "week"}[goal]
        # ★ 逐行校验，不能只看第一行。修之前只查 rows[0]：
        #   第二行若缺 cents/pct_bp，校验放行，表述层取值时 KeyError → 500，
        #   而且「回落确定性模板」这条兜底根本没机会触发。
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                return f"rows 第 {idx + 1} 项不是对象"
            if key not in row:
                return f"rows 第 {idx + 1} 项缺少 {key} 字段"
            if "cents" not in row:
                return f"rows 第 {idx + 1} 项缺少 cents 字段"
            if goal in ("category_breakdown", "top_merchants") and "pct_bp" not in row:
                return f"rows 第 {idx + 1} 项缺少 pct_bp 字段"
            # 下钻明细（top）是表述层第三个直接下标取值的地方，同样要逐项校验
            for t in (row.get("top") or []):
                if not isinstance(t, dict) or "merchant" not in t or "cents" not in t:
                    return f"rows 第 {idx + 1} 项的 top 明细格式不对（需要 merchant/cents）"
        # ★ 笔数也必须对得上。实测过的第二种「安静地答错」：
        #   模型把 count 填成了**类目数**（5），而表述层把它读成「共 5 笔支出」，
        #   合计又是对的——用户看到「近 30 天共 5 笔支出，合计 ¥10,912.00」，
        #   账上其实是 11 笔。结构全对，只有一个整数错，校验不看它就是放它过去。
        if goal == "category_breakdown":
            n = result.get("count")
            if not isinstance(n, int):
                return "缺少整数 count"
            if out_items and n != len(out_items):
                return f"笔数对不上：结果说 {n} 笔，输入实际有 {len(out_items)} 笔支出"
        elif goal == "top_merchants":
            n = result.get("count")
            if isinstance(n, int) and out_items and n != len(out_items):
                return f"笔数对不上：结果说 {n} 笔，输入实际有 {len(out_items)} 笔支出"
        return None

    if goal == "anomalies":
        for key in ("rows", "count", "flagged_cents", "scanned"):
            if key not in result:
                return f"缺少 {key}"
        rows = need_list("rows")
        if rows is None:
            return "rows 不是列表"
        # ★ 表述层对这四列做直接下标取值，缺一个就是 KeyError → 500。
        #   这里逐行挡住，让「回落确定性模板」有机会发生。
        for idx, row in enumerate(rows):
            for k in ("ts", "counterparty", "cents", "reasons"):
                if not isinstance(row, dict) or k not in row:
                    return f"rows 第 {idx + 1} 项缺少 {k} 字段"
        if out_items and need_int("scanned") and result["scanned"] < len(out_items):
            return f"只扫描了 {result['scanned']} 笔，输入实际有 {len(out_items)} 笔支出"

        # ★ 第二层：结构对了，判据也可能是坏的。
        #   真机联调实测：模型写了个「商户名里含白名单以外的字就算陌生商户」，
        #   于是瑞幸咖啡、美团外卖全被标成陌生商户 → 12 笔扫描、12 笔异常。
        #   结构完全合法，答案完全没用。下面两条是能在不重新实现业务判断的前提下
        #   抓到的两种塌陷：① 全中（分母没了）② 深夜/大额硬线索漏标。
        scanned = int(result.get("scanned") or 0)
        if scanned >= 3 and len(rows) >= scanned:
            return f"{scanned} 笔支出全部被标为异常，判定条件等于没生效"

        def hard_hit(item: dict) -> bool:
            """目标定义里写死的判定条件。这四条不依赖任何主观判断，
            少标一笔都说明生成的代码没按定义做。"""
            ts = str(item.get("ts") or "")
            hour = int(ts[11:13]) if len(ts) >= 13 else 12
            cp = str(item.get("counterparty") or "")
            return (hour < 6
                    or int(item.get("amount_cents") or 0) >= 100000
                    or "未知" in cp
                    or "境外" in cp)

        must = [i for i in out_items if hard_hit(i)]
        if must:
            # 用 (时间, 金额) 认账：模型常常把缺失的商户名改写成「未知商户」，
            # 拿商户名当键会误判成漏标，造成不必要的回落。
            flagged = {
                (str(r.get("ts") or ""), int(r.get("cents") or 0))
                for r in rows if isinstance(r, dict)
            }
            missed = [i for i in must
                      if (str(i.get("ts") or ""), int(i.get("amount_cents") or 0)) not in flagged]
            if missed:
                return f"有 {len(missed)} 笔深夜或大额交易没被标出，判据与目标定义不符"
        return None

    return None


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


__all__ = ["Goal", "generate", "llm_code", "preview", "template_code", "validate_result"]
