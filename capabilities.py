"""能力清单 —— 机器可读的「这个作品到底会干什么」。

    python capabilities.py            # 人看的表格
    python capabilities.py --json     # 机器读的 JSON（给自动评测脚本）

★ 为什么清单不是手写的 markdown：
   手写的清单一定会跟代码走散——文档写着「支持挂失」，代码里其实没有，
   这种漂移在评审前一周被发现是最难补的。
   所以这里直接读 `app.tools.REGISTRY`：**注册表就是清单**，
   新增一个工具会自动出现在清单里，删掉也会自动消失，不可能对不上。
"""

from __future__ import annotations

import json
import sys

from app.bank.seed import seed  # noqa: F401  （触发数据库初始化路径）
import app.tools  # noqa: F401  （导入即注册）
from app.tools.base import REGISTRY

SCENARIOS = [
    {
        "id": "transfer",
        "name": "智能转账",
        "requirement": "赛题 2.1 转账（即时/定时）",
        "tools": ["transfer", "query_balance"],
        "levels": {"query_balance": "green", "transfer": "yellow→red（日累计超限/首收款人）"},
    },
    {
        "id": "analysis",
        "name": "账单分析（沙箱执行模型生成的代码）",
        "requirement": "赛题 2.2 账单分析 + 第 45 行「代码需在沙箱中执行」",
        "tools": ["analyze_spending", "query_transactions"],
        "levels": {"query_transactions": "green", "analyze_spending": "green"},
    },
    {
        "id": "wealth",
        "name": "理财操作",
        "requirement": "赛题 2.3 理财申购（红色）",
        "tools": ["query_wealth_products", "purchase_wealth"],
        "levels": {"query_wealth_products": "green", "purchase_wealth": "red"},
    },
    {
        "id": "card",
        "name": "卡片管理",
        "requirement": "赛题 2.4 卡申请 / 交易限制解锁 / 挂失解挂",
        "tools": ["query_cards", "apply_virtual_card", "freeze_card", "unfreeze_card"],
        "levels": {
            "query_cards": "green",
            "apply_virtual_card": "yellow",
            "freeze_card": "red",
            "unfreeze_card": "red",
        },
    },
    {
        "id": "subscription",
        "name": "订阅 / 代扣管理",
        "requirement": "赛题 2.5 订阅查询与取消",
        "tools": ["query_subscriptions", "cancel_subscription"],
        "levels": {"query_subscriptions": "green", "cancel_subscription": "yellow"},
    },
    {
        "id": "linkage",
        "name": "跨场景联动（条件步骤 / 中断取消 / 人工接管）",
        "requirement": "赛题 2.6 多步任务与中断/取消/人工接管",
        "tools": [],
        "levels": {},
    },
]


def manifest() -> dict:
    tools = []
    for name in sorted(REGISTRY):
        m = REGISTRY[name]
        tools.append(
            {
                "name": name,
                "label": m.label,
                "base_level": m.base_level.value,
                "is_fund_movement": m.is_fund_movement,
                "requires_contact_resolution": m.resolves_contact,
                "min_kyc": m.min_kyc,
                "has_precheck": m.precheck is not None,
                "args_schema": {
                    k: str(v.get("type", "")) for k, v in (m.args_model.model_json_schema().get("properties") or {}).items()
                },
                "description": m.description,
            }
        )
    return {
        "project": "金衡 Statera",
        "thesis": "LLM 只提案，内核做裁决",
        "tool_count": len(tools),
        "tools": tools,
        "scenarios": SCENARIOS,
        "guarantees": [
            "所有资金与权限判定不经过任何模型输出",
            "风险等级只升不降（tools.base.escalate 无降级路径）",
            "任一层不通过即拒绝执行（fail-closed）",
            "用户可见的「已完成/余额为」全部由模板从 ToolResult 渲染，模型无权转述",
            "模型生成的代码在独立子进程 + 受限 builtins + 资源上限下执行",
        ],
    }


def _print_table(m: dict) -> None:
    print(f"\n{m['project']} · 能力清单（共 {m['tool_count']} 个已注册工具）")
    print("=" * 78)
    print(f"{'工具':<24}{'中文名':<16}{'基级':<8}{'动钱':<6}{'前置校验'}")
    print("-" * 78)
    for t in m["tools"]:
        print(
            f"{t['name']:<24}{t['label']:<16}{t['base_level']:<8}"
            f"{'是' if t['is_fund_movement'] else '—':<6}"
            f"{'有' if t['has_precheck'] else '—'}"
        )
    print("\n场景覆盖")
    print("-" * 78)
    for s in m["scenarios"]:
        have = "、".join(s["tools"]) or "（由以上工具组合而成）"
        print(f"· {s['name']}\n    {s['requirement']}\n    工具：{have}")
    print("\n架构保证")
    print("-" * 78)
    for g in m["guarantees"]:
        print(f"· {g}")
    print()


def main(argv: list[str]) -> int:
    m = manifest()
    if "--json" in argv:
        print(json.dumps(m, ensure_ascii=False, indent=2))
    else:
        _print_table(m)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
