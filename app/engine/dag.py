"""DAG 校验器 —— 结构与语义的第一道闸门。

LLM 产出的计划必须先过这里，任何一条不满足就直接拒绝，不进入裁决层。
这一层的意义：把「模型幻觉」转化成「一个被拒绝的结构化对象」，
而不是让它流进执行器。
"""

from __future__ import annotations

import re

from app.schemas import Plan
from app.tools.base import get_tool

STEP_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# 条件字段只允许 data 下的普通标识符路径，如 "count"、"sandbox.elapsed_ms"。
# 不允许下标、不允许函数调用——它只是取字段，不是一门表达式语言。
FIELD_RE = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)*$")
MAX_STEPS = 12

# ★ 身份类字段严禁出现在 args 里。
#   模型完全可以在 args 里塞 {"role": "admin"}：pydantic 默认忽略多余字段，
#   所以它今天不会生效（身份由 UserContext 从数据库拼装，任何工具都不接收身份入参）。
#   但「悄悄丢掉」和「明确拒绝」对评审是两件事：
#     · 悄悄丢掉 —— 你只能说「它不会有影响」，这是靠代码结构担保的推断；
#     · 明确拒绝 —— 你能现场把这条计划打出来，说「这条被 P-010 判为非法并记了审计」。
#   后者可演示、可复现，也把「以后某个新工具恰好声明了同名参数」这个隐患提前堵死。
_IDENTITY_KEYS = frozenset({
    "user_id", "uid", "user", "username", "role", "roles", "is_admin", "admin",
    "kyc", "kyc_level", "permissions", "identity", "verified", "mfa", "mfa_verified",
    "session_id", "trace_id", "token",
})


class DagError(ValueError):
    """计划非法。消息会原样写进审计日志。"""


def validate(plan: Plan) -> Plan:
    """校验通过则返回**拓扑排序后**的新计划；否则抛 DagError。"""
    if not plan.steps:
        raise DagError("计划为空")
    if len(plan.steps) > MAX_STEPS:
        raise DagError(f"步骤过多（{len(plan.steps)} > {MAX_STEPS}），疑似异常计划")

    ids = [s.id for s in plan.steps]
    if len(set(ids)) != len(ids):
        raise DagError("步骤 ID 重复")
    for sid in ids:
        if not STEP_ID_RE.match(sid):
            raise DagError(f"步骤 ID 非法：{sid}")

    id_set = set(ids)
    for s in plan.steps:
        if get_tool(s.tool) is None:
            raise DagError(f"步骤 {s.id} 使用了未注册的工具「{s.tool}」")
        # ★ 身份类字段不许混进参数：模型能写进来，但写进来就是一次显式的 Schema 拒绝。
        bad = sorted(k for k in s.args if str(k).strip().lower() in _IDENTITY_KEYS)
        if bad:
            raise DagError(
                f"步骤 {s.id} 的参数里出现身份类字段（{'、'.join(bad)}）——"
                f"身份只来自登录态，不接受任何来自自然语言的声明"
            )
        for dep in s.depends_on:
            if dep not in id_set:
                raise DagError(f"步骤 {s.id} 依赖了不存在的步骤 {dep}")
            if dep == s.id:
                raise DagError(f"步骤 {s.id} 依赖自身")

        # ---- 条件步骤 ----
        if s.when is not None:
            if s.when.step not in id_set:
                raise DagError(f"步骤 {s.id} 的条件引用了不存在的步骤 {s.when.step}")
            if s.when.step == s.id:
                raise DagError(f"步骤 {s.id} 的条件引用了自身")
            if not FIELD_RE.match(s.when.field):
                raise DagError(f"步骤 {s.id} 的条件字段非法：{s.when.field}")
            # ★ 条件引用的步骤必须先于本步骤执行，否则读到的是空结果。
            #   这里用「自动补一条依赖」而不是「报错」，因为补依赖只会让顺序更严格，
            #   不会放宽任何限制；真成环的话下面的环检测会拦下来。
            if s.when.step not in s.depends_on:
                s.depends_on.append(s.when.step)

    # Kahn 拓扑排序 + 环检测
    indeg = {s.id: len(s.depends_on) for s in plan.steps}
    children: dict[str, list[str]] = {s.id: [] for s in plan.steps}
    by_id = {s.id: s for s in plan.steps}
    for s in plan.steps:
        for dep in s.depends_on:
            children[dep].append(s.id)

    queue = [sid for sid, d in indeg.items() if d == 0]
    ordered: list[str] = []
    while queue:
        sid = queue.pop(0)
        ordered.append(sid)
        for ch in children[sid]:
            indeg[ch] -= 1
            if indeg[ch] == 0:
                queue.append(ch)

    if len(ordered) != len(ids):
        raise DagError("计划中存在循环依赖")

    return Plan(steps=[by_id[sid] for sid in ordered], summary=plan.summary)
