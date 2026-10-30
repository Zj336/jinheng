"""全局配置。所有可调参数集中在这里，方便写文档时逐条引用。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """读工程根目录的 `.env`（不存在就跳过）。

    ★ 为什么要有它：调用真模型需要 API Key，而 Key **绝不该写进代码或提交进仓库**。
      有了这个文件，演示时只要把 Key 填进 `.env`（已在 `.gitignore` 里），
      命令行就不必敲一长串 set，也不会有人把它顺手粘进源码。
    ★ 只补空缺：已经存在的环境变量优先级更高，方便临时用 `set` 覆盖。
    ★ 不引第三方库（python-dotenv），十几行手写够了——这个项目的依赖越少，
      评委在自己机器上跑通的概率越高。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(BASE_DIR / ".env")


def _i(key: str, default: int) -> int:
    return int(os.getenv(key, default))


@dataclass(frozen=True)
class Settings:
    # ---- 存储 ----
    db_path: str = os.getenv("JH_DB", str(BASE_DIR / "data" / "bank.db"))

    # ---- LLM ----
    # mock: 不联网的规则引擎，保证评审在无 API key 环境下也能跑通全流程
    llm_mode: str = os.getenv("JH_LLM_MODE", "mock")
    llm_base_url: str = os.getenv("JH_LLM_BASE_URL", "https://api.openai.com/v1")
    llm_api_key: str = os.getenv("JH_LLM_API_KEY", "")
    llm_model: str = os.getenv("JH_LLM_MODEL", "gpt-4o-mini")
    llm_timeout_s: float = float(os.getenv("JH_LLM_TIMEOUT", "30"))
    # 429/5xx 的重试次数（含首次）。真机联调时第一轮 12 句全挂，
    # 就是被一次限流打穿了——不重试的话「偶发」会被当成「不支持」。
    llm_retries: int = _i("JH_LLM_RETRIES", 3)
    # 输出上限。意图 + 计划是短 JSON，给太多只会拖慢：实测 DeepSeek 单轮 3~9 秒，
    # 其中相当一部分耗在「允许它写很长」上。
    llm_max_tokens: int = _i("JH_LLM_MAX_TOKENS", 512)

    # ---- 权限分级阈值（对应赛题「日累计 ≤ 1000 元」）----
    # 注意：比的是「日累计」，不是「单笔」。见 docs/权限分级规格.md
    #
    # ★ 2026-10-30 修正一条口径：**限额是硬约束，不是风险信号**。
    #   旧实现里「超过日限额」只是把等级升到 RED，而 RED 可以被短信验证码放行——
    #   结果就是 1000 元的日限额，输个验证码就能突破。强验证解决的是
    #   「你是本人吗」，它不能解决「这笔钱本来就不许转」。
    #   现在四道限额外加一道单笔强验证阈值：
    #     单笔 ≥ 500 元      → 强验证（不是拒绝，只是加一道身份确认）
    #     单日累计 > 1000 元 → 拒绝（P-040，不可被强验证覆盖）
    #     单月累计 > 20000 元→ 拒绝（P-042）
    #     同一收款人当日 > 1000 元 → 拒绝（P-043）
    daily_transfer_limit_cents: int = _i("JH_DAILY_LIMIT_CENTS", 100_000)      # 1000.00 元/日
    monthly_transfer_limit_cents: int = _i("JH_MONTHLY_LIMIT_CENTS", 2_000_000)  # 20000.00 元/月
    per_contact_daily_limit_cents: int = _i("JH_CONTACT_DAILY_LIMIT_CENTS", 100_000)  # 1000.00 元/收款人/日
    # 单笔超过这个数就要强验证（旧名字叫 single_confirm_limit，一直没被用上——
    # 2026-10-30 才接进裁决，「配置里有、代码里不用」本身就是个坑）
    single_confirm_limit_cents: int = _i("JH_CONFIRM_LIMIT_CENTS", 50_000)    # 500.00 元

    # ---- 熔断 ----
    circuit_fail_threshold: int = _i("JH_CIRCUIT_FAILS", 3)
    circuit_inject_threshold: int = _i("JH_CIRCUIT_INJECTS", 4)

    # ---- 沙箱 ----
    sandbox_timeout_s: float = float(os.getenv("JH_SANDBOX_TIMEOUT", "5"))
    sandbox_mem_mb: int = _i("JH_SANDBOX_MEM_MB", 256)


settings = Settings()
