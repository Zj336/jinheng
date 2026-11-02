"""一键自检 —— 一条命令跑完三套验收，给出一个总判决。

    python selfcheck.py

它做的事就是把下面三条串起来，再加一条：任何一条挂了，退出码非 0。

    python demo.py          # 端到端场景（含断言）
    python -m pytest -q     # 单元与安全用例
    python eval_report.py   # 对抗评估 + 沙箱攻击面

★ 为什么要有它：评审/队友最容易犯的错是「跑了 demo 就以为全绿了」。
  三个脚本各自只覆盖一部分，单看任何一个都可能漏。这里让漏不了。
  输出里会逐条打印被调用的命令和它的关键结论，便于把结论直接贴进报告。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_check_tmp = tempfile.TemporaryDirectory(prefix="statera-selfcheck-")

# ★ 自检自己也要说人话。
#   `python selfcheck.py` 的 stdout 在 Windows 上跟着控制台代码页走（cp936），
#   而本文件满屏中文，于是「三项全 PASS」却被打印成 `��� Statera �� һ���Լ�`。
#   崩溃还能读堆栈，乱码连猜都难——评审第一眼看到的就是它。
#   reconfigure 只改编码不改行为；旧解释器没有该方法时静默跳过，
#   errors="replace" 保证这里永远不会反过来把自检搞挂。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

# ★ 必须显式让子进程也用 UTF-8 输出。
#   我们自己按 utf-8 解码子进程的输出，但子进程的 stdout 一旦是管道，
#   Windows 上就会退回系统本地编码（cp936）——⏎ ✓ 这类字符根本编码不了，
#   子进程直接 UnicodeEncodeError 退出 1。
#   现象极具迷惑性：手敲命令一切正常，一跑自检就「有失败项」。
_CHILD_ENV = {
    **os.environ,
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
    # ★ 自检的三个子进程一律走 mock：自检回答的是「系统对不对」，不是「模型今天发挥如何」。
    #   联调期间 .env 里可能配着真模型，验收结论不能跟着模型措辞漂移。
    "JH_LLM_MODE": "mock",
    "JH_DB": str(Path(_check_tmp.name) / "bank.db"),
}


def _maybe_reexec_in_venv() -> None:
    """当前解释器没装依赖时，自动改用项目自带的 .venv 重跑自己。

    ★ 为什么要有它：子进程一律用 sys.executable 启动。评审在自己机器上
      没激活 venv 就直接 `python selfcheck.py`（这太正常了），pytest 那一步
      会输出 `No module named pytest`，自检从输出里抓不到 `N passed`，
      最后显示 `[FAIL] 单元与安全用例 pytest  （未匹配到）`——
      一个明明全绿的项目被环境问题判成失败，而且看不出原因。
      演示当天现场出这种失败最伤。
    """
    if os.environ.get("JH_SELFCHECK_REEXEC") == "1":
        return  # 已经切过一次，不再递归
    try:
        import fastapi  # noqa: F401
        import httpx  # noqa: F401
        import pydantic  # noqa: F401
        import pytest  # noqa: F401
        import yaml  # noqa: F401
        return  # 依赖齐了，原地跑
    except ImportError:
        pass

    venv_py = ROOT / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    if not venv_py.exists():
        return  # 没有 .venv 可切，交给 preflight 报一条能看懂的错误
    if Path(sys.executable).resolve() == venv_py.resolve():
        return  # 已经在 venv 里还缺依赖 = 装漏了，同样交给 preflight

    print(f"[环境] 当前解释器缺依赖，自动切到项目虚拟环境：{venv_py}", flush=True)
    # ★ 用子进程而不是 os.execv：execv 在 Windows 上会换成新的句柄，
    #   `python selfcheck.py > log.txt` 这种重定向下来的输出会整段丢掉
    #   （表现：日志里只剩这一行，看起来像自检什么都没跑）。
    #   子进程直接继承父进程的 stdout，重定向、管道、IDE 面板都正常。
    try:
        rc = subprocess.call(
            [str(venv_py), str(Path(__file__).resolve()), *sys.argv[1:]],
            env={**os.environ, "JH_SELFCHECK_REEXEC": "1"},
        )
    except OSError:
        return  # 切不过去就原地跑，preflight 会给出人话
    raise SystemExit(rc)


def _preflight() -> list[str]:
    """返回缺失的依赖名。空列表 = 环境可用。"""
    missing = []
    for mod in ("fastapi", "pydantic", "httpx", "pytest", "yaml"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    return missing


def _run(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_CHILD_ENV,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _grab(pattern: str, text: str, default: str = "（未匹配到）") -> str:
    m = re.search(pattern, text)
    return m.group(0) if m else default


def main() -> int:
    _maybe_reexec_in_venv()

    print("金衡 Statera · 一键自检\n" + "=" * 70)

    missing = _preflight()
    if missing:
        print(f"\n  [FAIL] 运行环境\n         当前解释器缺少依赖：{', '.join(missing)}")
        print(f"         解释器路径：{sys.executable}")
        print("\n         请先激活项目虚拟环境再跑（否则单测这一步必定失败）：")
        print("           Windows:      .venv\\Scripts\\activate")
        print("           macOS/Linux:  source .venv/bin/activate")
        print("         还没建环境的话：python -m venv .venv && pip install -r requirements.txt")
        print("\n" + "=" * 70)
        print("总判决：环境不全，未开始验收")
        return 1

    checks: list[tuple[str, bool, str]] = []

    code, out = _run(["demo.py"])
    summary = _grab(r"\d+/\d+ 通过", out.replace("\x1b", ""))
    checks.append(("端到端场景 demo.py", code == 0, summary))

    code, out = _run(["-m", "pytest", "tests", "-q"])
    summary = _grab(r"\d+ passed(?:, \d+ warning)?", out.replace("\x1b", ""))
    checks.append(("单元与安全用例 pytest", code == 0, summary))

    code, out = _run(["eval_report.py"])
    plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
    # 3.11 的 f-string 里不能出现反斜杠，所以先把三段抓出来
    defend = _grab(r"金衡防守成功率 [\d/.]+ = [\d.]+%", plain)
    baseline = _grab(r"对照系统 \d+/\d+ = [\d.]+%", plain)
    sandbox = _grab(r"沙箱闸门 \d+/\d+ 符合预期", plain)
    checks.append((
        "对抗评估 eval_report.py", code == 0,
        "防御 " + defend + "；" + baseline + "；" + sandbox,
    ))

    # ★ 必须另指定输出名：默认目标是 validation/extended.json，
    #   那是随包提交的评测证据。跑一次自检就把它重写掉，
    #   等于「评委一验收，证据就变了」——虽然 mock 下内容一致，但说不清。
    code, out = _run(["evaluate.py", "--json", "validation/selfcheck_extended.json"])
    checks.append(("消融与正常业务 evaluate.py", code == 0,
                   "逐条结果见 validation/selfcheck_extended.json"))

    print()
    for name, ok, detail in checks:
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}\n         {detail}")

    ok_all = all(ok for _, ok, _ in checks)
    print("\n" + "=" * 70)
    print("总判决：" + ("全部通过" if ok_all else "有失败项，见上"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
