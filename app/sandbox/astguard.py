"""代码静态闸门 —— 子进程之前的第一道关。

★ 为什么静态检查还不够，但我们仍然要做：
   静态检查挡不住所有逃逸（比如通过字符串拼接构造属性名），所以真正的
   边界是子进程 + 受限 builtins + 超时。但静态检查能把 99% 的「明显不该
   跑的东西」在**不启动进程**的情况下拒掉，既快又能写进审计日志。

判据是白名单思维：不认识的一律拒。
"""

from __future__ import annotations

import ast
import hashlib

MAX_CODE_CHARS = 6000
MAX_NODES = 3000

# 禁止导入的模块（连尝试都不允许）
BANNED_MODULES = {
    "os", "sys", "subprocess", "socket", "shutil", "pathlib", "importlib",
    "ctypes", "builtins", "pickle", "marshal", "multiprocessing", "threading",
    "requests", "urllib", "http", "ftplib", "sqlite3", "tempfile", "glob",
    "io", "code", "pty", "signal", "resource", "gc", "inspect", "types",
}

# 禁止直接调用的名字
BANNED_NAMES = {
    "eval", "exec", "compile", "open", "__import__", "input", "breakpoint",
    "globals", "locals", "vars", "dir", "getattr", "setattr", "delattr",
    "memoryview", "object",
}


class CodeRejected(ValueError):
    """代码未通过静态闸门。"""


def _digest(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]


def validate_code(code: str) -> str:
    """通过则返回 sha256 前 16 位，否则抛 CodeRejected。"""
    if len(code) > MAX_CODE_CHARS:
        raise CodeRejected(f"代码长度 {len(code)} 超过上限 {MAX_CODE_CHARS}")
    if "\x00" in code:
        raise CodeRejected("代码含空字节")

    try:
        tree = ast.parse(code, filename="<analysis>", mode="exec")
    except SyntaxError as exc:
        raise CodeRejected(f"语法错误：{exc.msg}（第 {exc.lineno} 行）") from exc

    nodes = 0
    for node in ast.walk(tree):
        nodes += 1
        if nodes > MAX_NODES:
            raise CodeRejected(f"AST 节点数超过上限 {MAX_NODES}")

        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = (node.module if isinstance(node, ast.ImportFrom) else None) or (
                node.names[0].name if node.names else ""
            )
            root = mod.split(".")[0]
            raise CodeRejected(f"禁止 import：{root}（白名单要求零依赖）")

        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise CodeRejected(f"禁止访问双下划线属性：{node.attr}")

        if isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            raise CodeRejected(f"禁止使用名称：{node.id}")

        # 沙箱里的代码不需要处理异常以外的控制流逃逸，但禁止 with/try 之外
        # 一切会触碰文件系统的表达式：这里主要拦 f-string 里的诡异写法不现实，
        # 所以把「打开文件」相关的内建已经 ban 掉了，此处不再重复。

    return _digest(code)
