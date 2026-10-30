"""代码执行沙箱。见 runner.py 顶部的四道闸门说明。"""

from app.sandbox.runner import SandboxError, SandboxResult, run_code

__all__ = ["SandboxError", "SandboxResult", "run_code"]
