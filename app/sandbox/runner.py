"""★ 代码执行沙箱 —— 独立子进程，四道闸门。

    静态闸门（astguard） → 隔离进程（-I 隔离模式、空 cwd）
      → 受限 builtins（无 open / 无 import / 无 eval）
      → 资源闸门（超时 + RSS 内存看门狗 + 输出上限）

★ 设计立场：**不指望拦住所有逃逸，要求的是「逃逸的代价受控」**。
   哪怕代码写出了我们没预料到的东西，它也只能：
     - 拿到本次传入的那份数据副本（不是数据库，不是文件系统）
     - 活到超时为止（默认 5 秒）
     - 吃满内存上限之前（默认 256MB）
   并且它看不到任何密钥 —— 子进程的环境变量是重建的，不是继承的。

★ 已知局限（必须写进安全自评报告）：
   1. 内存看门狗靠 50ms 轮询 RSS，不是内核级硬限；瞬时冲高可能越过上限。
   2. Windows 无 seccomp/CGroup，隔离强度弱于 Linux 容器方案。
   3. 未做网络命名空间隔离；由于禁止 import socket 且无第三方库，
      实际可达路径为零，但这属于「事实上的」而非「机制上的」保证。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass

from app.config import settings
from app.sandbox.astguard import CodeRejected, validate_code

MAX_OUTPUT_BYTES = 64 * 1024
_RSS_POLL_S = 0.05


def _sandbox_interpreter() -> str:
    """挑一个「起进程就是它本身」的解释器。

    ★ 踩过的坑：venv 里的 `Scripts/python.exe` 在某些机器上是包装器，
      真正跑代码的是它再 fork 出来的孙进程。后果很严重：
        - 超时 kill 只杀掉外壳，孙进程继续跑（资源泄漏）
        - 内存看门狗读的是外壳的 RSS，看到的永远是 4MB
      基础解释器没有这层跳转；而且沙箱用 `-I` 隔离模式，本来就不该
      依赖 venv 里的三方包 —— 只有标准库才应该是分析代码能碰的东西。
    """
    for cand in (getattr(sys, "_base_executable", None), sys.executable):
        if cand and os.path.isfile(cand):
            return cand
    return sys.executable


_INTERPRETER = _sandbox_interpreter()

# 子进程里可用的内建函数 —— 只留纯计算用的，一个 I/O 相关的都没有
_BOOTSTRAP = r'''
import json, sys

SAFE = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "divmod": divmod, "enumerate": enumerate, "filter": filter, "float": float,
    "int": int, "isinstance": isinstance, "len": len, "list": list, "map": map,
    "max": max, "min": min, "range": range, "reversed": reversed, "round": round,
    "set": set, "sorted": sorted, "str": str, "sum": sum, "tuple": tuple,
    "zip": zip,
    "Exception": Exception, "ValueError": ValueError, "KeyError": KeyError,
    "TypeError": TypeError, "ZeroDivisionError": ZeroDivisionError,
    "IndexError": IndexError, "AttributeError": AttributeError,
}

payload = json.loads(sys.stdin.read())
g = {"__builtins__": SAFE, "data": payload["data"], "result": None}
try:
    exec(compile(payload["code"], "<analysis>", "exec"), g)
except Exception as exc:
    sys.stdout.write(json.dumps(
        {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)},
        ensure_ascii=False))
    sys.exit(0)

out = g.get("result")
sys.stdout.write(json.dumps({"ok": True, "result": out}, ensure_ascii=False, default=str))
'''


@dataclass
class SandboxResult:
    ok: bool
    result: object = None
    error: str = ""
    elapsed_ms: int = 0
    code_digest: str = ""
    killed: str = ""          # "" / "timeout" / "memory" / "output" / "static"


class SandboxError(RuntimeError):
    pass


# ------------------------------------------------------------------ 内存看门狗

if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes as _wt

    class _MemCounters(ctypes.Structure):
        _fields_ = [
            ("cb", _wt.DWORD),
            ("PageFaultCount", _wt.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]


def _rss_mb(pid: int) -> float | None:
    """Windows：取进程 WorkingSetSize。其他平台返回 None（用超时兜底）。

    ★ 坑：必须显式声明 restype/argtypes。ctypes 默认把返回值当 32 位 int，
      而 64 位 Windows 的 HANDLE 是指针 —— 不声明就会被截断，OpenProcess
      可能「成功」拿到一个完全不相干的进程句柄，于是读到的是别人的内存
      （表现为：子进程明明吃了 300MB，却恒定读回 4MB）。
    """
    if sys.platform != "win32":
        return None

    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [_wt.DWORD, _wt.BOOL, _wt.DWORD]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]

    PROCESS_QUERY_INFORMATION, PROCESS_VM_READ = 0x0400, 0x0010
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return None
    try:
        c = _MemCounters()
        c.cb = ctypes.sizeof(c)
        get_info = ctypes.windll.psapi.GetProcessMemoryInfo
        get_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MemCounters), _wt.DWORD]
        get_info.restype = _wt.BOOL
        if not get_info(h, ctypes.byref(c), c.cb):
            return None
        return c.WorkingSetSize / (1024 * 1024)
    finally:
        k32.CloseHandle(h)


def _kill_tree(proc: subprocess.Popen) -> None:
    """连子孙一起杀。Windows 用 taskkill /T；其他平台尽力而为。"""
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
            )
        else:
            proc.kill()
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


def _watch_memory(proc: subprocess.Popen, limit_mb: int, state: dict, stop: threading.Event) -> None:
    while not stop.wait(_RSS_POLL_S):
        if proc.poll() is not None:
            return
        mb = _rss_mb(proc.pid)
        if mb is not None and mb > limit_mb:
            state["killed"] = "memory"
            _kill_tree(proc)
            return


# ------------------------------------------------------------------ 主入口

def _child_env(tmpdir: str) -> dict[str, str]:
    """重建环境变量，而不是继承 —— 保证密钥不出现在子进程里。"""
    env = {"PYTHONIOENCODING": "utf-8", "TEMP": tmpdir, "TMP": tmpdir}
    for k in ("SYSTEMROOT", "SYSTEMDRIVE", "PATH", "COMSPEC", "WINDIR"):
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def run_code(code: str, data: dict, timeout_s: float | None = None,
             mem_mb: int | None = None) -> SandboxResult:
    timeout_s = settings.sandbox_timeout_s if timeout_s is None else timeout_s
    mem_mb = settings.sandbox_mem_mb if mem_mb is None else mem_mb

    t0 = time.perf_counter()

    # ---- 闸门 1：静态检查（不启进程）----
    try:
        digest = validate_code(code)
    except CodeRejected as exc:
        return SandboxResult(
            ok=False, error=f"代码未通过静态安全检查：{exc}",
            elapsed_ms=int((time.perf_counter() - t0) * 1000), killed="static",
        )

    payload = json.dumps({"code": code, "data": data}, ensure_ascii=False)
    cmd = [_INTERPRETER, "-I", "-X", "utf8", "-c", _BOOTSTRAP]

    tmpdir = tempfile.mkdtemp(prefix="jh_sbx_")
    state: dict = {"killed": ""}
    stop = threading.Event()
    timed_out = False

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            cwd=tmpdir, env=_child_env(tmpdir),
        )
    except OSError as exc:
        return SandboxResult(ok=False, error=f"沙箱进程启动失败：{exc}", code_digest=digest)

    wd = threading.Thread(target=_watch_memory, args=(proc, mem_mb, state, stop), daemon=True)
    wd.start()

    try:
        out, err = proc.communicate(input=payload, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        out, err = proc.communicate()
    finally:
        stop.set()

    elapsed = int((time.perf_counter() - t0) * 1000)

    if timed_out:
        return SandboxResult(ok=False, error=f"代码执行超过 {timeout_s:g} 秒，已强制终止",
                             elapsed_ms=elapsed, code_digest=digest, killed="timeout")
    if state["killed"] == "memory":
        return SandboxResult(ok=False, error=f"代码占用内存超过 {mem_mb}MB，已强制终止",
                             elapsed_ms=elapsed, code_digest=digest, killed="memory")
    if len(out.encode("utf-8")) > MAX_OUTPUT_BYTES:
        return SandboxResult(ok=False, error=f"输出超过 {MAX_OUTPUT_BYTES // 1024}KB 上限，已丢弃",
                             elapsed_ms=elapsed, code_digest=digest, killed="output")

    if not out.strip():
        detail = (err or "").strip().splitlines()[-1:] or ["（无输出）"]
        return SandboxResult(ok=False, error=f"沙箱无返回：{detail[0]}",
                             elapsed_ms=elapsed, code_digest=digest)

    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        return SandboxResult(ok=False, error="沙箱输出不是合法 JSON",
                             elapsed_ms=elapsed, code_digest=digest)

    if not parsed.get("ok"):
        return SandboxResult(ok=False, error=parsed.get("error", "分析代码执行出错"),
                             elapsed_ms=elapsed, code_digest=digest)

    return SandboxResult(ok=True, result=parsed.get("result"),
                         elapsed_ms=elapsed, code_digest=digest)
