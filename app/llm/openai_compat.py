"""兼容 OpenAI /chat/completions 协议的 LLM 适配器。

适用于 OpenAI、DeepSeek、通义、智谱、vLLM、Ollama 等一切兼容端点。
没有配置 key 时构造函数不报错，首次调用才失败——这样服务本身仍能启动，
方便评审先看界面再决定要不要接模型。
"""

from __future__ import annotations

import json

import httpx

from app.config import settings
from app.engine.guard import InputEnvelope
from app.llm.base import LLMAdapter, Proposal
from app.schemas import Intent, Plan, Step
from app.tools.base import tool_names

SYSTEM_PROMPT = """你是银行智能体的「理解与规划」模块。
你的唯一职责：把用户的话解析成结构化意图，并规划成有序的任务 DAG。
你没有任何执行权限，也不决定风险等级——那是后端策略引擎的事。

可用工具（只能用这些，不得自创）：
{tools}

输出严格的 JSON，不要任何解释文字或 markdown 代码块：
{{
  "intents": [{{"name": "<工具名>", "slots": {{}}, "missing": [], "confidence": 0.0}}],
  "steps": [{{"id": "s1", "tool": "<工具名>", "args": {{}}, "depends_on": []}}],
  "clarifications": ["需要用户补充的信息"]
}}

规则：
- slot 金额一律用「分」为单位的整数（500 元 → 50000）。
- 信息不足时填 missing 并给出 clarifications，不要臆造。
- 步骤 id 用 s1、s2……，形成有向无环图。
"""


class OpenAICompatLLM(LLMAdapter):
    name = "openai_compat"

    def __init__(self) -> None:
        self.base_url = settings.llm_base_url.rstrip("/")
        self.api_key = settings.llm_api_key
        self.model = settings.llm_model

    def propose(self, envelope: InputEnvelope) -> Proposal:
        if not self.api_key:
            return Proposal(clarifications=["未配置 LLM API Key，请设置 JH_LLM_API_KEY 或改用 mock 模式。"])

        system = SYSTEM_PROMPT.format(tools=", ".join(tool_names()))
        # ★ 用户输入走独立的 user 消息，并被包进 untrusted 信封，
        #   绝不拼进 system prompt。
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": envelope.to_llm_block()},
        ]

        try:
            with httpx.Client(timeout=settings.llm_timeout_s) as client:
                resp = client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": self.model,
                        "messages": messages,
                        "temperature": 0,
                        "response_format": {"type": "json_object"},
                    },
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001
            return Proposal(clarifications=[f"理解模块暂时不可用（{type(exc).__name__}），请稍后再试。"])

        return self._to_proposal(content)

    def _to_proposal(self, content: str) -> Proposal:
        try:
            obj = json.loads(content)
        except json.JSONDecodeError:
            return Proposal(clarifications=["理解模块返回了无法解析的结果，已中止本次请求。"])

        intents = [
            Intent(
                name=str(i.get("name", "unknown")),
                slots=dict(i.get("slots") or {}),
                missing=list(i.get("missing") or []),
                confidence=float(i.get("confidence", 1.0)),
            )
            for i in obj.get("intents", [])
        ]
        steps = [
            Step(
                id=str(s.get("id", f"s{n + 1}")),
                tool=str(s.get("tool", "")),
                args=dict(s.get("args") or {}),
                depends_on=list(s.get("depends_on") or []),
            )
            for n, s in enumerate(obj.get("steps", []))
        ]
        return Proposal(
            intents=intents,
            plan=Plan(steps=steps, summary=""),
            clarifications=list(obj.get("clarifications") or []),
            raw=content,
        )
