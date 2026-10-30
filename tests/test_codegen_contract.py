"""分析代码的「产出契约」验收。

这条线不是安全边界，是**正确性边界**：模型可以自由写代码，但只要它产出的结构
对不上表述层，用户看到的就会是一句语气正常的错话。

真实事故（2026-10-30 真机联调）：模型生成的代码算出的合计完全正确，
但它把结果组织成 {"window_days": 30, "breakdown": [...]}，而表述层读的是 rows，
于是「近 30 天没有支出记录」被打印出来 —— 账上明明有 12 笔（11 笔支出）。
没有异常、没有日志，**安静地答错比直接崩溃危险得多**。
"""

from __future__ import annotations

import pytest

from app.bank.db import connect, recent_transactions
from app.engine.render import render_result
from app.sandbox import codegen
from app.sandbox.runner import run_code
from app.schemas import UserContext
from app.tools.analysis import AnalyzeArgs, _analyze

ITEMS = [
    {"ts": "2026-10-20T14:00:00", "direction": "out", "amount_cents": 1800,
     "counterparty": "瑞幸咖啡", "category": "餐饮", "memo": "美式"},
    {"ts": "2026-10-19T10:00:00", "direction": "in", "amount_cents": 5000,
     "counterparty": "公司", "category": "收入", "memo": "报销"},
    {"ts": "2026-10-18T21:00:00", "direction": "out", "amount_cents": 6200,
     "counterparty": "美团外卖", "category": "餐饮", "memo": "晚餐"},
]

# 模型真实写出过的那种结构：数字对、键名不对
WRONG_SHAPE = (
    "out = [i for i in data['items'] if i['direction'] == 'out']\n"
    "result = {'window_days': data['days'],\n"
    "          'total_out_cents': sum(i['amount_cents'] for i in out),\n"
    "          'breakdown': []}\n"
)


@pytest.mark.parametrize("goal", ["category_breakdown", "top_merchants", "anomalies", "trend"])
def test_template_output_satisfies_its_own_contract(goal):
    """模板是回落基线，它自己必须先合格——否则回落等于把用户扔进第二层错。"""
    outcome = run_code(codegen.template_code(goal), {"days": 30, "items": ITEMS})
    assert outcome.ok, outcome.error
    assert codegen.validate_result(goal, outcome.result, ITEMS) is None


def test_missing_rows_is_rejected_with_a_human_reason():
    outcome = run_code(WRONG_SHAPE, {"days": 30, "items": ITEMS})
    assert outcome.ok, outcome.error
    reason = codegen.validate_result("category_breakdown", outcome.result, ITEMS)
    assert reason and "rows" in reason


def test_count_must_be_transactions_not_categories():
    """实测事故：模型把 count 填成了**类目数**。

    合计是对的（¥10,912.00），只有这一个整数错——表述层读出「近 30 天共 5 笔支出」，
    账上其实 11 笔。结构全对，校验不看这个数就放它过去了。
    """
    result = {"rows": [{"category": "餐饮", "cents": 8000, "count": 2, "pct_bp": 10000, "top": []}],
              "count": 1, "total_cents": 8000}   # 3 笔支出，却报 1 笔
    reason = codegen.validate_result("category_breakdown", result, ITEMS)
    assert reason and "笔数" in reason


def test_empty_result_is_rejected_when_there_are_expenses():
    """空结果不能靠「没有数据」蒙混过去：输入里明明有支出。"""
    result = {"rows": [], "count": 0, "total_cents": 0}
    assert codegen.validate_result("category_breakdown", result, ITEMS)


def test_anomalies_must_declare_how_many_rows_it_scanned():
    """扫描数少于输入笔数 = 漏扫，报「未发现异常」就是假阴性。"""
    result = {"rows": [], "count": 0, "flagged_cents": 0, "scanned": 1}
    reason = codegen.validate_result("anomalies", result, ITEMS)
    assert reason and "2" in reason


# 真机联调实测到的第二种塌陷：结构完全合法，但判据是坏的。
# 模型写了个「商户名含白名单以外的字就算陌生商户」，中文商户名全中招 ——
# 12 笔扫描、12 笔异常。下面的用例把这两种塌陷钉住。
BULK = [
    {"ts": "2026-10-20T14:00:00", "direction": "out", "amount_cents": 1800, "counterparty": "瑞幸咖啡"},
    {"ts": "2026-10-20T12:00:00", "direction": "out", "amount_cents": 3500, "counterparty": "美团外卖"},
    {"ts": "2026-10-20T09:00:00", "direction": "out", "amount_cents": 6800, "counterparty": "肯德基"},
    {"ts": "2026-10-19T03:10:00", "direction": "out", "amount_cents": 500000, "counterparty": "未知商户"},
]


def test_anomalies_rejects_flagging_everything():
    """全中 = 判定条件等于没生效。分母没了，结论就没有信息量。"""
    rows = [{"ts": i["ts"], "counterparty": i["counterparty"], "cents": i["amount_cents"],
             "reasons": ["陌生商户"]} for i in BULK]
    result = {"rows": rows, "count": len(rows), "flagged_cents": sum(r["cents"] for r in rows),
              "scanned": len(BULK)}
    reason = codegen.validate_result("anomalies", result, BULK)
    assert reason and "全部" in reason


def test_anomalies_rejects_missing_a_deep_night_large_transaction():
    """深夜 + 大额是目标定义里写死的硬线索，漏掉任何一笔都算判据跑偏。"""
    rows = [{"ts": "2026-10-20T14:00:00", "counterparty": "瑞幸咖啡", "cents": 1800,
             "reasons": ["陌生商户"]}]
    result = {"rows": rows, "count": 1, "flagged_cents": 1800, "scanned": len(BULK)}
    reason = codegen.validate_result("anomalies", result, BULK)
    assert reason and "没被标出" in reason


def test_anomalies_accepts_a_partial_but_correct_result():
    """不能滥杀：标对了硬线索、额外少标几笔「陌生商户」是允许的判断差异。"""
    rows = [{"ts": "2026-10-19T03:10:00", "counterparty": "未知商户", "cents": 500000,
             "reasons": ["深夜交易", "大额"]}]
    result = {"rows": rows, "count": 1, "flagged_cents": 500000, "scanned": len(BULK)}
    assert codegen.validate_result("anomalies", result, BULK) is None


# ------------------------------------------------------------ 回落路径

def test_tool_falls_back_to_template_and_says_so(monkeypatch):
    """模型代码违反契约时：换模板重算，并把「换过」写进 sandbox 元数据。"""
    monkeypatch.setattr(codegen, "generate", lambda goal, days: (WRONG_SHAPE, "llm"))

    with connect() as conn:
        ctx = UserContext(user_id="u_1001", session_id="t")
        r = _analyze(conn, ctx, AnalyzeArgs(goal="category_breakdown", days=30))

    assert r.ok, r.error
    sbx = r.data["sandbox"]
    assert sbx["generator"] == "template(fallback)"
    assert sbx["fallback"]["from"] == "llm"
    assert "rows" in sbx["fallback"]["reason"]
    # 数字必须来自模板重算，而不是把错结构原样透传
    assert r.data["result"]["total_cents"] > 0
    assert r.data["result"]["rows"]

    text = render_result(r)
    assert "已改用确定性模板" in text, "回落必须写在用户看得见的地方"


def test_tool_does_not_fall_back_when_model_code_is_correct(monkeypatch):
    """不该滥杀：合格的模型产物必须原样采用，否则「模型生成代码」就成了一句空话。"""
    good = codegen.template_code("category_breakdown")
    monkeypatch.setattr(codegen, "generate", lambda goal, days: (good, "llm"))

    with connect() as conn:
        ctx = UserContext(user_id="u_1001", session_id="t")
        r = _analyze(conn, ctx, AnalyzeArgs(goal="category_breakdown", days=30))

    assert r.ok, r.error
    assert r.data["sandbox"]["generator"] == "llm"
    assert "fallback" not in r.data["sandbox"]


def test_seed_user_actually_has_expenses():
    """上一条出错方式的前提是「账上真有钱」——先把这个前提钉住，免得用例自欺。"""
    with connect() as conn:
        rows = recent_transactions(conn, "u_1001", 30)
    assert any(r["direction"] == "out" for r in rows)
