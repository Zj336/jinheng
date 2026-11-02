"""命令行入口的统一编码初始化。

★ 为什么需要这个模块：
  在中文 Windows 上，脚本的 stdout 一旦是**管道或重定向**（IDE 面板、CI、
  `python demo.py > log.txt`、以及大多数评测机），Python 会退回系统代码页
  cp936/GBK。本项目满屏中文，还带 ✓ ✅ ¥ 这类字符，于是直接：

      UnicodeEncodeError: 'gbk' codec can't encode character '\\u2705'

  现象极具迷惑性：**在终端里手敲一切正常，加个重定向就崩**，而且崩在
  打印结果的那一行——看起来像功能坏了，其实是编码。

  这个 bug 曾是真实的：`demo.py` / `eval_report.py` / `llm_smoke.py`
  直接跑都会崩（`selfcheck.py` 侥幸没事，因为它给子进程注入了
  PYTHONIOENCODING=utf-8，等于把问题藏在了一层子进程后面）。
  所以现在每个入口脚本第一件事就是调它。

  只改编码、不改行为；旧解释器没有 reconfigure 时静默跳过；
  errors="replace" 保证这段代码自己永远不会反过来把脚本搞挂。
"""

from __future__ import annotations

import sys


def init_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
