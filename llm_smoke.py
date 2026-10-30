"""真模型联调冒烟表 —— 回答一个问题：**接了真模型，它到底听懂几句？**

用法（在工程根目录）：

    copy .env.example .env      :: 然后填 JH_LLM_MODE=openai 等四行
    .venv\\Scripts\\python.exe llm_smoke.py

    .venv\\Scripts\\python.exe llm_smoke.py --mock-only   :: 不接模型，自查脚本本身能跑

★ 为什么需要这个脚本：`demo.py` / `pytest` / `eval_report.py` 为了可复现，
  一律强制走 mock（见各文件顶部）。也就是说**它们永远不会调用真模型**。
  如果只看它们全绿，就会得出「真模型也一定行」的错误结论——
  而这恰恰是评委最想问的那个问题。所以单独一个脚本，只做联调，不做验收。

它**不设断言**，只出对照表：
  · 同一批口语，mock 与真模型各自解析成什么
  · 真模型这一侧有没有崩（超时 / JSON 不通 / 编了不存在的工具）
  · 平均耗时（决定演示时会不会卡）

判定口径（与讨论记录 05 一致）：
  ✅ 办到    —— 有工具真的执行了
  🟡 待确认  —— 进入了确认/验证环节，说明听懂了，只是要人点头
  🟠 规则拒  —— 被策略引擎挡下（听懂了，但这件事不许做）
  ❌ 听不懂  —— 回复里是「没听懂 / 不理解 / 不在能力范围」
  💥 异常    —— 超时 / 网络错 / 返回不是合法 JSON
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# ★ 必须放在任何 app 导入之前：settings 在 import 时就定值了。
#   用独立的库文件，避免把联调产生的脏数据写进演示库。
os.environ.setdefault("JH_DB", str(BASE_DIR / "data" / "smoke.db"))

from app.bank.db import init_db  # noqa: E402
from app.bank.seed import seed  # noqa: E402
from app.engine.orchestrator import Orchestrator  # noqa: E402
from app.llm import set_adapter  # noqa: E402
from app.llm.mock import MockLLM  # noqa: E402

USER = "u_1001"

# 12 句「评审真的会这么问」的口语。别改成书面语——书面语是模型最舒服的输入，
# 用书面语测出来的通过率没有参考价值。
SENTENCES = [
    "我上个月点外卖花了多少钱",
    "这个月是不是超支了",
    "我那张信用卡额度还有多少",
    "帮我看看有没有乱扣费",
    "最近消费正常吗",
    "我有哪些理财",
    "把爱奇艺退了吧",
    "再给他转100",
    "明天把房租转给房东",
    "帮我把钱都归拢一下",
    "我有哪些卡",
    "余额够的话给王强转500",
]

C_RED, C_GRN, C_YEL, C_ORG, C_DIM, C_END, C_B = (
    "\033[91m", "\033[92m", "\033[93m", "\033[33m", "\033[90m", "\033[0m", "\033[1m"
)


def classify(r) -> tuple[str, str]:
    """把一个 TurnResult 归到五类。返回 (标记, 判定名)。

    ★ 口径要说清楚，因为它直接决定「听懂率」这个数字：
      · 「待确认」算听懂 —— 系统理解了，只是要人点一下头；
      · 「规则拒」也算听懂 —— 理解了，但这件事按规则不许做；
      · 「追问」**不算**听懂 —— 这是「话说了一半」，属于没完成。
      讨论记录 05 里那 6/12 用的是同一口径，两处数字可以直接对比。
    """
    text = r.text or ""
    # 先看 pending：带条件的计划会「先执行查询、再等服务确认」，两个列表都非空，
    # 这时更该报的是「等人点头」而不是「办完了」。
    if getattr(r, "pending", None):
        return f"{C_YEL}🟡{C_END}", "待确认"
    if getattr(r, "executed", None):
        tools = ",".join(e["step"]["tool"] for e in r.executed)
        return f"{C_GRN}✅{C_END}", f"办到({tools})"
    if getattr(r, "denied", None):
        hits = ",".join(r.denied[0].get("policy_hits") or [])
        # P-001 = 工具不在白名单，本质是「这功能我没有」，与听不懂等价
        return (f"{C_ORG}🟠{C_END}", f"规则拒({hits})") if "P-001" not in hits \
            else (f"{C_RED}❌{C_END}", "听不懂(工具不支持)")
    # 「接不住」是 mock 词表耗尽时说的话，属于**听不懂**，不是追问。
    # 它同样会带 clarify（mock 把帮助文本放在 clarifications 里），所以这条必须先判。
    if any(k in text for k in ("没听懂", "听不懂", "不理解", "接不住")):
        return f"{C_RED}❌{C_END}", "听不懂"
    if getattr(r, "clarify", None):
        # 由引擎直接告诉我们是追问，而不是靠回复里有没有「还差」去猜 ——
        # 真模型生成的追问不含这两个字，会被错记成「其他(说明类)」。
        return f"{C_DIM}🟡{C_END}", "追问(缺槽位)"
    return f"{C_DIM}·{C_END}", "其他(说明类)"


def first_line(text: str, n: int = 60) -> str:
    line = (text or "").strip().split("\n")[0]
    return line if len(line) <= n else line[:n] + "…"


def run_all(adapter, tag: str) -> list[dict]:
    set_adapter(adapter)
    orch = Orchestrator()
    out: list[dict] = []
    for i, sent in enumerate(SENTENCES, 1):
        t0 = time.perf_counter()
        try:
            r = orch.handle_message(USER, f"smoke_{tag}_{i}", sent)
            ms = (time.perf_counter() - t0) * 1000
            mark, verdict = classify(r)
            out.append({"sent": sent, "mark": mark, "verdict": verdict,
                        "line": first_line(r.text), "ms": ms, "error": None})
        except Exception as exc:  # noqa: BLE001 —— 联调脚本要的正是「把异常抓出来继续跑」
            ms = (time.perf_counter() - t0) * 1000
            out.append({"sent": sent, "mark": f"{C_RED}💥{C_END}", "verdict": f"异常({type(exc).__name__})",
                        "line": str(exc)[:80], "ms": ms, "error": exc})
    return out


def rate(rows: list[dict]) -> float:
    """一次听懂率 = （办到 + 待确认 + 规则拒）/ 总数。追问与听不懂都不计入。"""
    ok = sum(
        1 for r in rows
        if r["verdict"].startswith(("办到", "待确认", "规则拒"))
    )
    return ok / max(1, len(rows)) * 100


def breakdown(rows: list[dict]) -> str:
    n = len(rows)
    ask = sum(1 for r in rows if r["verdict"].startswith("追问"))
    bad = sum(1 for r in rows if r["verdict"].startswith(("听不懂", "异常")))
    return f"追问 {ask} / 听不懂或异常 {bad} / 共 {n}"


def main() -> int:
    mock_only = "--mock-only" in sys.argv

    init_db()
    seed(reset=True, quiet=True)

    mock_rows = run_all(MockLLM(), "mock")

    if mock_only:
        print(f"{C_B}mock 自查（--mock-only：不接真模型）{C_END}")
        for r in mock_rows:
            print(f"  {r['mark']} {r['sent']:<18} {r['verdict']:<20} {C_DIM}{r['line']}{C_END}")
        print(f"\n脚本本身正常。一次听懂率 {rate(mock_rows):.0f}%（{breakdown(mock_rows)}）")
        return 0

    from app.config import settings

    if settings.llm_mode == "mock" or not settings.llm_api_key:
        print(f"{C_RED}还没配真模型。{C_END}")
        print("  1) copy .env.example .env")
        print("  2) 在 .env 里填 JH_LLM_MODE=openai / JH_LLM_BASE_URL / JH_LLM_API_KEY / JH_LLM_MODEL")
        print("  3) 重跑本脚本。只想确认脚本能用，加 --mock-only。")
        return 2

    print(f"{C_B}模型{C_END}  {settings.llm_base_url}  ·  {settings.llm_model}")
    print(f"{C_DIM}同一批 {len(SENTENCES)} 句口语，先 mock 后真模型，并排看差异{C_END}\n")

    t0 = time.perf_counter()
    real_rows = run_all(_real_adapter(), "real")
    total_ms = (time.perf_counter() - t0) * 1000

    for m, r in zip(mock_rows, real_rows):
        print(f"{C_B}{r['sent']}{C_END}")
        print(f"   mock {m['mark']} {m['verdict']:<22} {C_DIM}{m['line']}{C_END}")
        print(f"   real {r['mark']} {r['verdict']:<22} {C_DIM}{r['line']}{C_END}")
        print(f"        {C_DIM}{r['ms']:.0f} ms{C_END}")

    errs = [r for r in real_rows if r["error"] or r["verdict"].startswith("异常")]
    print(f"\n{C_B}汇总{C_END}")
    print(f"  mock 一次听懂率 {rate(mock_rows):5.1f}%   {C_DIM}{breakdown(mock_rows)}{C_END}")
    print(f"  真模型一次听懂率 {rate(real_rows):5.1f}%   {C_DIM}{breakdown(real_rows)}{C_END}")
    print(f"  {C_DIM}口径同讨论记录 05：追问与听不懂都不计入{C_END}")
    print(f"  平均耗时 {total_ms / len(SENTENCES):.0f} ms/句   峰值 {max(r['ms'] for r in real_rows):.0f} ms")
    if errs:
        print(f"  {C_RED}异常 {len(errs)} 句 —— 先把这 {len(errs)} 句修掉，别急着看听懂率{C_END}")
        for r in errs:
            print(f"    · {r['sent']} → {r['line']}")
    else:
        print(f"  {C_GRN}无超时 / 无非法 JSON / 无未注册工具{C_END}")
    return 0


def _real_adapter():
    from app.llm.openai_compat import OpenAICompatLLM

    return OpenAICompatLLM()


if __name__ == "__main__":
    raise SystemExit(main())
