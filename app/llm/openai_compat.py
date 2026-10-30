"""兼容 OpenAI /chat/completions 协议的 LLM 适配器。

适用于 OpenAI、DeepSeek、通义、智谱、vLLM、Ollama 等一切兼容端点。
没有配置 key 时构造函数不报错，首次调用才失败——这样服务本身仍能启动，
方便评审先看界面再决定要不要接模型。

★ 2026-10-30 真机联调（DeepSeek）改了三处，都是「第一次接真模型才发现」的：

  1. **工具参数表要喂给模型**。原来只给了工具**名字**，模型知道「有 cancel_subscription」
     却不知道它要一个 `merchant` 参数，于是发出一个缺参的步骤，被 Schema 校验拒成
     P-010 ——用户看到的是「参数不合法（merchant: Field required）」，像是系统坏了，
     其实是提示词没把契约说清楚。现在直接从注册表生成参数表，**与工具定义同源**。

  2. **失败要说人话**。原来任何异常都被压成「理解模块暂时不可用（HTTPStatusError）」——
     12 句全挂时，看到这句话根本不知道该查 key、查余额还是查网络。
     现在带上状态码和端点返回的原文；429/5xx 还会退避重试两次。

  3. **条件步骤与默认值写进提示词**。「余额够就转」这类两跳请求，模型原本只做第一步；
     而「近 30 天」这种窗口，模型会反过来追问用户。两件事都该在提示词里定死。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from app.config import settings
from app.engine.guard import InputEnvelope
from app.llm.base import LLMAdapter, Proposal
from app.schemas import Intent, Plan, Step, StepCondition
from app.tools.base import REGISTRY, tool_names

# 拿不准时不该由模型自创的参数约定。写在这里而不是散落在提示词各段，
# 是为了改的时候只改一处。
ARG_HINTS = {
    "to": "收款人的姓名或别名，如「妈」「王强」；用户说「他/她」且没有明确指代就填 missing",
    "card": "卡号后四位，如「2233」；用户没说哪张卡就填 missing，不要猜",
    "merchant": "订阅/商户名称，如「爱奇艺」",
    "product": "理财产品名称或关键词，如「稳健增利90天」",
    "amount_cents": "金额，单位是**分**的整数（500 元 → 50000）",
    "days": "回溯天数。用户没给时间范围时**默认 30**，不要追问",
    "goal": "分析目标，只能取 category_breakdown / top_merchants / anomalies / trend；"
            "拿不准就用 category_breakdown",
    "schedule_date": "预约日期，格式 YYYY-MM-DD",
    "account_kind": "current（活期）或 savings（储蓄），默认 current",
}


def _tool_table() -> str:
    """从工具注册表生成参数表。

    ★ 与 `REGISTRY` 同源，不多写一份：工具加了参数，提示词自动跟上。
      硬编码两份清单的下场是——改了一处忘了另一处，模型开始发缺参步骤。
    """
    lines: list[str] = []
    for meta in REGISTRY.values():
        schema = meta.args_model.model_json_schema() if meta.args_model else {}
        props: dict = schema.get("properties", {})
        required = set(schema.get("required", []))
        args = []
        for name, spec in props.items():
            kind = spec.get("type") or ("enum" if "enum" in spec else "any")
            if "enum" in spec:
                kind = "|".join(str(v) for v in spec["enum"])
            args.append(f"{name}:{kind}{'*' if name in required else ''}")
        risk = "资金" if meta.is_fund_movement else "非资金"
        lines.append(f"- {meta.name}({', '.join(args)})  [{risk}] {meta.description}")
    return "\n".join(lines)


SYSTEM_PROMPT = """你是银行智能体的「理解与规划」模块。
你的唯一职责：把用户的话解析成结构化意图，并规划成有序的任务 DAG。
你没有任何执行权限，也不决定风险等级——那是后端策略引擎的事。

可用工具（带 * 的参数是必填）：
{tools}

参数约定：
{hints}

输出严格的 JSON（json object），不要任何解释文字或 markdown 代码块：
{{
  "intents": [{{"name": "<工具名>", "slots": {{}}, "missing": [], "confidence": 0.0}}],
  "steps": [{{"id": "s1", "tool": "<工具名>", "args": {{}}, "depends_on": []}}],
  "clarifications": ["需要用户补充的信息"]
}}

规则：
- 金额一律用「分」为单位的整数（500 元 → 50000）。
- **能用默认值就不要追问**：时间范围默认 30 天，账户默认活期。
  只有在「不给就做不了」时才填 missing 并给出 clarifications（例如转账没给收款人）。
- 缺参数的意图也要放进 intents，并在 missing 里列出缺哪些字段。
- 步骤 id 用 s1、s2……，形成有向无环图。
- **条件式请求要写成两跳**。「余额够的话给王强转 500」= 先查余额，再条件转账：

  {{"steps": [
    {{"id": "s1", "tool": "query_balance", "args": {{}}}},
    {{"id": "s2", "tool": "transfer", "args": {{"to": "王强", "amount_cents": 50000}},
      "depends_on": ["s1"],
      "when": {{"step": "s1", "field": "balance_cents", "op": ">=", "value": 50000}}}}
  ]}}

  when 只允许 {{step, field, op, value}} 这四项，op 只能是 > >= < <= == !=，
  **不允许写任何表达式字符串**。条件不成立时引擎会跳过该步骤，不需要你在文本里解释。
- 用户说的和工具无关（闲聊、问你能干什么）时，intents 与 steps 都留空数组，
  clarifications 里给一句自然语言回答。
"""


class LLMError(RuntimeError):
    """带上「HTTP 状态码 + 端点原话」的失败，而不是一个类型名。"""


def _err_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return (resp.text or "").strip()[:200]
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)[:200]
    return str(body)[:200]


_RETRY_STATUS = {429, 500, 502, 503, 504}


class OpenAICompatLLM(LLMAdapter):
    name: str = "openai_compat"

    def __init__(self) -> None:
        self.base_url = settings.llm_base_url.rstrip("/")
        self.api_key = settings.llm_api_key
        self.model = settings.llm_model
        self.timeout = settings.llm_timeout_s
        self.retries = max(1, int(settings.llm_retries))
        self.max_tokens = int(settings.llm_max_tokens)

    # ---------------------------------------------------------------- HTTP

    def _post(self, payload: dict) -> dict:
        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        last: str = ""
        for attempt in range(self.retries):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(url, headers=headers, json=payload)
            except httpx.TimeoutException:
                last = f"请求超时（>{self.timeout:.0f}s）"
            except httpx.HTTPError as exc:
                last = f"网络异常（{type(exc).__name__}）"
            else:
                if resp.status_code < 400:
                    return resp.json()
                last = f"HTTP {resp.status_code}：{_err_text(resp)}"
                if resp.status_code not in _RETRY_STATUS:
                    raise LLMError(last)      # 4xx 里除了 429，重试没有意义
            if attempt < self.retries - 1:
                time.sleep(1.5 * (attempt + 1))
        raise LLMError(last or "未知错误")

    # ---------------------------------------------------------------- 主流程

    def propose(self, envelope: InputEnvelope) -> Proposal:
        if not self.api_key:
            return Proposal(clarifications=["未配置 LLM API Key，请设置 JH_LLM_API_KEY 或改用 mock 模式。"])

        system = SYSTEM_PROMPT.format(
            tools=_tool_table(),
            hints="\n".join(f"- {k}：{v}" for k, v in ARG_HINTS.items()),
        )
        # ★ 用户输入走独立的 user 消息，并被包进 untrusted 信封，
        #   绝不拼进 system prompt。
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": envelope.to_llm_block()},
        ]

        try:
            data = self._post({
                "model": self.model,
                "messages": messages,
                "temperature": 0,
                "max_tokens": self.max_tokens,
                "response_format": {"type": "json_object"},
            })
            content = data["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001
            # ★ 把「怎么坏的」原样说出来。演示当天遇到 401，用户需要知道是 key 的问题，
            #   而不是对着「理解模块暂时不可用」猜半天。
            return Proposal(
                clarifications=[f"理解模块暂时不可用：{exc}"],
                raw="",
            )

        try:
            return self._to_proposal(content)
        except Exception as exc:  # noqa: BLE001
            # ★ 模型输出千奇百怪（返回数组、confidence 写成「高」、args 写成字符串）。
            #   修之前这些都会冒到 FastAPI 变成裸 500，并且 `connect()` 的 rollback
            #   会把这一轮的审计（含注入命中）一起抹掉——出错的那一轮恰好没有记录。
            #   现在一律降级成「这轮没听懂」，审计照写，用户可换个说法重来。
            return Proposal(
                clarifications=[f"理解模块返回了无法使用的结构，已中止本次请求（{type(exc).__name__}）。"],
                raw=content or "",
            )

    def _to_proposal(self, content: str) -> Proposal:
        content = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", (content or "").strip())
        try:
            obj = json.loads(content)
        except json.JSONDecodeError:
            return Proposal(clarifications=["理解模块返回了无法解析的结果，已中止本次请求。"], raw=content)

        if not isinstance(obj, dict):
            # 模型返回了 `[1,2]` 这种顶层数组：结构不对，整轮作废，不猜。
            return Proposal(
                clarifications=["理解模块返回的不是一个 JSON 对象，已中止本次请求。"], raw=content
            )

        def _as_dict(v: Any) -> dict:
            return v if isinstance(v, dict) else {}

        def _as_list(v: Any) -> list:
            return v if isinstance(v, list) else []

        intents: list[Intent] = []
        for i in _as_list(obj.get("intents")):
            if not isinstance(i, dict):
                continue
            try:
                conf = float(i.get("confidence", 1.0))
            except (TypeError, ValueError):
                conf = 1.0
            intents.append(Intent(
                name=str(i.get("name", "unknown")),
                slots=_as_dict(i.get("slots")),
                missing=[str(m) for m in _as_list(i.get("missing"))],
                confidence=min(1.0, max(0.0, conf)),
            ))

        steps: list[Step] = []
        notes: list[str] = []
        for n, raw_step in enumerate(_as_list(obj.get("steps"))):
            if not isinstance(raw_step, dict):
                continue
            when = None
            if raw_step.get("when") is not None:
                try:
                    when = StepCondition(**raw_step["when"])
                except Exception:  # noqa: BLE001
                    # ★ 条件解析不出来就**丢掉这一步**，而不是当作无条件执行。
                    #   把一个「余额够就转」偷偷变成「无条件转」，是最坏的一种失败。
                    notes.append("计划里有一个条件步骤的条件不合法，已跳过该步骤（不会无条件执行）。")
                    continue
            steps.append(Step(
                id=str(raw_step.get("id") or f"s{n + 1}"),
                tool=str(raw_step.get("tool", "")),
                args=_as_dict(raw_step.get("args")),
                depends_on=[str(d) for d in _as_list(raw_step.get("depends_on"))],
                when=when,
            ))

        return Proposal(
            intents=intents,
            plan=Plan(steps=steps, summary=""),
            clarifications=[str(c) for c in _as_list(obj.get("clarifications"))] + notes,
            raw=content,
        )


__all__ = ["OpenAICompatLLM", "LLMError", "SYSTEM_PROMPT", "tool_names"]
