"""沙箱的「牙齿」验收。

这些用例不是在测功能，是在测**逃逸代价受控**：
恶意代码要么根本跑不起来（静态闸门），要么跑起来也拿不到东西、活不长。
"""

from __future__ import annotations

import pytest

from app.sandbox.astguard import CodeRejected, validate_code
from app.sandbox.runner import run_code

SAMPLE = {
    "days": 30,
    "items": [
        {"ts": "2026-10-20T14:00:00", "direction": "out", "amount_cents": 1800,
         "counterparty": "瑞幸咖啡", "category": "餐饮", "memo": "美式"},
        {"ts": "2026-10-19T10:00:00", "direction": "in", "amount_cents": 5000,
         "counterparty": "公司", "category": "收入", "memo": "报销"},
    ],
}


# ------------------------------------------------------------ 闸门一：静态

@pytest.mark.parametrize(
    "code",
    [
        "import os\nresult = 1",
        "from os import system\nresult = 1",
        "result = __import__('os').getcwd()",
        "result = open('/etc/passwd').read()",
        "result = eval('1+1')",
        "result = ().__class__.__bases__",
        "result = globals()",
    ],
)
def test_dangerous_code_rejected_statically(code):
    with pytest.raises(CodeRejected):
        validate_code(code)


def test_syntax_error_rejected():
    with pytest.raises(CodeRejected):
        validate_code("def f(:\n  pass")


# ------------------------------------------------------------ 闸门二：执行

def test_legit_code_runs_and_returns_result():
    code = "out = [i for i in data['items'] if i['direction'] == 'out']\nresult = {'n': len(out), 'sum': sum(i['amount_cents'] for i in out)}"
    r = run_code(code, SAMPLE)
    assert r.ok, r.error
    assert r.result == {"n": 1, "sum": 1800}


def test_data_is_a_copy_not_a_live_handle():
    """子进程改 data 不影响父进程 —— 它拿到的是序列化副本。"""
    code = "data.clear()\nresult = 1"
    r = run_code(code, SAMPLE)
    assert r.ok, r.error
    assert SAMPLE["items"], "父进程数据被改动了"


def test_exception_inside_code_is_reported_not_crashed():
    r = run_code("result = 1 / 0", SAMPLE)
    assert not r.ok
    assert "ZeroDivisionError" in r.error


def test_infinite_loop_is_killed_by_timeout():
    r = run_code("while True:\n    pass\n", SAMPLE, timeout_s=0.8)
    assert not r.ok
    assert r.killed == "timeout"


def test_memory_hog_is_killed_by_watchdog():
    code = "a = []\nwhile True:\n    a.append('x' * 1000000)\n"
    r = run_code(code, SAMPLE, timeout_s=10, mem_mb=64)
    assert not r.ok
    # 必须由内存看门狗终止，而不是熬到 10 秒超时 —— 后者说明看门狗没在工作
    assert r.killed == "memory", f"看门狗未生效，killed={r.killed!r}"
    assert r.elapsed_ms < 5000


def test_oversized_output_is_rejected():
    code = "result = 'x' * (200 * 1024)"
    r = run_code(code, SAMPLE)
    assert not r.ok
    assert r.killed == "output"


def test_child_env_has_no_secrets(monkeypatch):
    """子进程的环境变量是重建的，不继承父进程的密钥。"""
    from app.sandbox.runner import _child_env

    monkeypatch.setenv("JH_LLM_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "also-not")
    env = _child_env("C:/tmp")
    assert "JH_LLM_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
