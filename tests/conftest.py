import os

import pytest

# ★ 用例必须确定性：即使 .env 里配了真模型（联调期间就是），跑测试也一律走 mock。
#   否则同一条用例这次过、下次不过，失败原因变成「模型这句话怎么措辞」——
#   那是 NLU 质量指标，不是回归测试该盯的东西。真模型的表现另有 llm_smoke.py 衡量。
os.environ["JH_LLM_MODE"] = "mock"

from app.bank.seed import seed  # noqa: E402
from app.engine import session as session_store  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_state():
    """每个用例前重置数据库和会话态，保证用例之间零耦合。"""
    seed(reset=True)
    for sid in list(getattr(session_store, "_sessions", {})):
        session_store.clear(sid)
    yield
