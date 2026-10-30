"""一键启动。

    python run.py            # 起 HTTP 服务（默认 8000 端口）
    python run.py 8200       # 起服务，换一个端口
    python run.py demo       # 跑内置演示（不需要起服务）
    python run.py seed       # 重置演示数据
"""

from __future__ import annotations

import subprocess
import sys

DEFAULT_PORT = "8000"


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else "serve"

    if arg == "seed":
        from app.bank.seed import seed

        seed()
        return

    if arg == "demo":
        from demo import main as demo_main

        demo_main()
        return

    # 本机还跑着别的服务时，8000 会被占掉——注意 Windows 允许两个进程用不同绑定
    # 地址同时监听同一个端口（一个 0.0.0.0、一个 127.0.0.1），谁抢到看运气，
    # 于是同一个链接可能打开别人的页面。换端口比杀进程安全。
    port = arg if arg.isdigit() else DEFAULT_PORT

    subprocess.run(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--reload", "--port", port],
        check=False,
    )


if __name__ == "__main__":
    main()
