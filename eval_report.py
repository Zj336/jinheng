"""安全对抗评估报告。

    python eval_report.py

对同一批攻击用例，对比两种实现方式的防守成功率。
这份输出可以直接截图进技术文档与答辩 PPT。
"""

from __future__ import annotations

import os
import sys

# ★ 评估报告必须可复现：对照系统的成绩要写进技术文档，不能被「今天模型发挥如何」左右。
os.environ["JH_LLM_MODE"] = "mock"

from app.eval.runner import run_all, summarize

C_RED, C_GRN, C_DIM, C_END, C_B = "\033[91m", "\033[92m", "\033[90m", "\033[0m", "\033[1m"


def _sandbox_section() -> bool:
    """第二张表：生成代码的沙箱闸门。

    ★ 单独成表的原因：这一类攻击没有「对照系统」可比 —— 对照系统压根不执行
      生成代码。所以这里衡量的不是「比别人强」，而是「我们自己守不守得住」。
    """
    from app.eval.runner import run_sandbox_cases

    print(f"{C_B}生成代码沙箱 · 攻击面验证{C_END}")
    print(f"{C_DIM}前提：账单分析执行的是模型生成的代码，即不可信输入{C_END}\n")

    results = run_sandbox_cases()
    for r in results:
        mark = f"{C_GRN}拦住{C_END}" if r.contained else f"{C_RED}失守{C_END}"
        print(f"  [{mark}] {r.case_id}  {r.desc:<22}{C_DIM}预期={r.expect:<9}实测={r.actual}{C_END}")

    bad = sum(1 for r in results if not r.contained)
    total = len(results)
    good = total - bad
    print()
    print(f"{C_GRN if bad == 0 else C_RED}{C_B}沙箱闸门 {good}/{total} 符合预期{C_END}\n")
    return bad == 0


def main() -> None:
    print(f"\n{C_B}金衡 Statera · 安全对抗评估{C_END}")
    print(f"{C_DIM}对照系统：把权限规则写进 Prompt 的典型实现（复刻，非参赛作品）{C_END}\n")

    results = run_all()
    s = summarize(results)

    print(f"{C_B}{'攻击类别':<10}{'用例':>5}{'金衡':>10}{'对照系统':>12}{'对照失守金额':>14}{C_END}")
    print("─" * 58)

    order = ["提示注入", "越权访问", "额度绕过", "参数注入", "会话伪造"]
    for cat in order:
        rows = s.by_cat.get(cat, [])
        if not rows:
            continue
        j = sum(1 for r in rows if r.jinheng_defended)
        b = sum(1 for r in rows if r.baseline_defended)
        leak = sum(r.leaked_amount_cents for r in rows)
        jc = C_GRN if j == len(rows) else C_RED
        bc = C_GRN if b == len(rows) else C_RED
        # 两系统都满分的类别无区分度，如实标出，不计入优势
        tag = f" {C_DIM}← 无区分度{C_END}" if (j == len(rows) and b == len(rows)) else ""
        print(f"{cat:<10}{len(rows):>5}{jc}{j:>6}/{len(rows):<3}{C_END}"
              f"{bc}{b:>9}/{len(rows):<3}{C_END}"
              f"{('¥%.2f' % (leak / 100)):>14}{tag}")

    print("─" * 58)
    tj, tb = s.defended("jinheng"), s.defended("baseline")
    t = s.total
    leak = sum(r.leaked_amount_cents for r in results)
    print(f"{C_B}{'合计':<10}{t:>5}{C_GRN}{tj:>6}/{t:<3}{C_END}"
          f"{C_RED}{tb:>9}/{t:<3}{C_END}{('¥%.2f' % (leak / 100)):>14}{C_END}")

    # 明细：对照系统失守、金衡守住的用例
    diff = [r for r in results if r.baseline_defended is False and r.jinheng_defended]
    if diff:
        print(f"\n{C_B}对照系统失守、金衡拦下的用例（节选 {min(8, len(diff))} 条）{C_END}")
        for r in diff[:8]:
            print(f"  {C_DIM}[{r.case_id}]{C_END} {' → '.join(r.turns[:2])}")
            print(f"        {C_RED}对照系统转出了 {r.baseline_executed} 笔，共 "
                  f"¥{r.leaked_amount_cents / 100:.2f}{C_END}；金衡拦下")

    print()
    ok = tj == t
    print(f"{C_GRN if ok else C_RED}{C_B}金衡防守成功率 {tj}/{t} = {tj / t * 100:.1f}%{C_END}"
          f"   |   对照系统 {tb}/{t} = {tb / t * 100:.1f}%\n")

    # —— 如实声明：哪些类别不构成本方案的证据 ——
    flat = [c for c in order if s.by_cat.get(c)
            and sum(1 for r in s.by_cat[c] if r.jinheng_defended) == len(s.by_cat[c])
            and sum(1 for r in s.by_cat[c] if r.baseline_defended) == len(s.by_cat[c])]
    if flat:
        print(f"{C_DIM}注：{'、'.join(flat)} 两系统均全数拦下，差异不来自权限架构。"
              f"\n    其原因是这些 payload 在语义解析层就未能构成可执行转账参数，"
              f"\n    属于 NLU 健壮性而非授权正确性，如实列出、不计入本方案优势。{C_END}\n")

    sandbox_ok = _sandbox_section()
    sys.exit(0 if (ok and sandbox_ok) else 1)


if __name__ == "__main__":
    main()
