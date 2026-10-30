"""全系统的数据契约。

设计原则：所有在模块之间流动的数据都必须是强类型的。
LLM 与确定性内核之间只通过这里的模型通信——LLM 无法产出未定义的字段，
这就是「模型只提案」在类型层面的第一道约束。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- 风险分级

class RiskLevel(str, Enum):
    """赛题第 30-35 行的三色分级。"""

    GREEN = "green"    # 自动执行：纯查询
    YELLOW = "yellow"  # 用户确认：小额转账、订阅取消、虚拟卡申请
    RED = "red"        # 强验证：大额转账、改密、挂失、理财申购


class DecisionOutcome(str, Enum):
    """裁决结果。注意它和 RiskLevel 不是一一对应：
    GREEN 也可能被判 DENY（比如越权访问他人账户）。"""

    ALLOW = "allow"        # 放行，直接执行
    CONFIRM = "confirm"    # 放行，但需用户显式确认
    MFA = "mfa"            # 放行，但需多因子认证
    DENY = "deny"          # 拒绝执行


# ---------------------------------------------------------------- 意图与计划

class Intent(BaseModel):
    """理解层产物。LLM 只被允许填这个结构，不允许直接调用任何工具。"""

    name: str = Field(description="意图名，必须是 tools.registry 中注册过的")
    slots: dict[str, Any] = Field(default_factory=dict, description="结构化槽位")
    missing: list[str] = Field(default_factory=list, description="缺失的必填槽位")
    ambiguities: list[str] = Field(default_factory=list, description="需要向用户澄清的点")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    raw_utterance: str = ""


class StepCondition(BaseModel):
    """条件步骤的判定条件 —— 跨场景联动的开关。

    ★ 为什么它不是一段表达式字符串：
      如果 when 写成 `"data['count'] > 0"` 这种自由文本，就等于让模型
      **生成代码**来参与决策——而这正是本项目花最大力气避免的事。
      这里只允许「引用哪个步骤 + 取哪个字段 + 什么比较符 + 跟什么字面量比」，
      执行器做的是纯比较，没有 eval、没有解析器、没有可注入的语法。

    ★ 求值失败一律**判为不成立**（跳过该步骤），不做「猜一下应该是真的」。
    """

    step: str = Field(description="引用的前序步骤 id")
    field: str = Field(description="取该步骤结果 data 下的字段，支持 a.b")
    op: Literal[">", ">=", "<", "<=", "==", "!="] = ">"
    value: int | float | str | bool = 0


class Step(BaseModel):
    """任务 DAG 的一个节点 = 一次原子工具调用。"""

    id: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    # 该步骤是否属于「资金类」操作，由 DAG 校验器依据工具元数据回填
    is_fund_movement: bool = False
    # 条件步骤：条件不成立就不执行。为 None 表示无条件执行。
    when: StepCondition | None = None


class Plan(BaseModel):
    """规划层产物：一张有向无环图。"""

    steps: list[Step] = Field(default_factory=list)
    summary: str = ""

    def get(self, step_id: str) -> Step | None:
        return next((s for s in self.steps if s.id == step_id), None)


# ---------------------------------------------------------------- 执行上下文

class UserContext(BaseModel):
    """裁决层做判断所需的全部「客观事实」。

    关键点：这里没有任何一项来自 LLM。全部由确定性的数据库查询拼装，
    因此模型无法通过话术影响裁决结果。
    """

    user_id: str
    session_id: str
    trace_id: str = ""
    kyc_level: int = 1
    # ★ 角色只能来自登录态（令牌签发时写入），永远不来自对话。
    #   「我是银行管理员」在类型层面就进不了这里——本字段由 auth.issue_token
    #   从数据库读出，用户说什么都改不了它。
    role: str = "customer"
    # 当日已发生的转账累计（分），由 DB 实时计算（月累计在裁决时按需查库）
    daily_transferred_cents: int = 0
    # 本会话已通过的多因子凭证，例如 {"mfa": "sms", "verified_at": ...}
    verified_factors: list[str] = Field(default_factory=list)
    # ★ 凭证的作用范围：它只放行**这一个** step_id。
    #   空串表示凭证不对应任何步骤（此时一律不放行）。
    #   「一次确认 = 一步」这件事必须由数据结构表达，不能靠调用方自觉。
    verified_for_step: str = ""
    # 本会话累计被拒次数 / 注入特征命中次数（熔断用）
    denied_count: int = 0
    injection_hits: int = 0
    # 会话是否已被熔断锁定为只读
    locked: bool = False


# ---------------------------------------------------------------- 裁决

class Decision(BaseModel):
    step_id: str
    tool: str
    level: RiskLevel
    outcome: DecisionOutcome
    reason: str
    policy_hits: list[str] = Field(default_factory=list, description="命中的策略规则 ID")
    required: list[str] = Field(default_factory=list, description="需要补充的认证方式")
    # ★ 裁决所依据的**结构化事实**：收款人姓名 / 银行 / 尾号、金额、手续费、
    #   到账方式、当日剩余额度……全部来自数据库查询，模型碰不到。
    #   有了它，确认卡片才能把「对谁、对哪个账户、做多少钱的事」完整回显。
    #   没有它的时候，「给妈转一万」的确认卡片只写「向「妈」转账」——
    #   别名背后是哪个账户，用户在最该看见的那一刻反而看不见。
    facts: dict[str, Any] = Field(
        default_factory=dict, description="裁决依据的结构化事实（供表述层回显）"
    )


# ---------------------------------------------------------------- 执行

class ToolResult(BaseModel):
    """执行层产物。表述层只能基于这个对象渲染用户可见文本。"""

    ok: bool
    tool: str = ""
    step_id: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class ChatReply(BaseModel):
    """一次完整对话的返回。"""

    session_id: str
    text: str
    pending: list[Decision] = Field(default_factory=list, description="待确认/待验证的步骤")
    trace_id: str = ""
