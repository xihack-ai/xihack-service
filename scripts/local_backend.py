"""本地启动：先检查 SSH 通道，隐藏输入 PG 密码，只读验证后启动 API。"""

import argparse
import getpass
import json
import os
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="检查数据库并启动本地后端（密码不落盘）")
    parser.add_argument("--check-only", action="store_true", help="仅验证数据库，不启动服务")
    parser.add_argument("--port", type=int, default=8000, help="本地 HTTP 端口")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("HTTP 端口必须在 1 到 65535 之间")

    try:
        from dotenv import load_dotenv
        from app.database import DatabaseCheckError, DatabaseSettings, inspect_database
    except ImportError:
        print('缺少项目依赖，请先运行：.\\.venv\\Scripts\\python.exe -m pip install -e ".[dev]"')
        return 1

    # 固定项目根目录，避免相对知识库路径随终端位置变化。
    os.chdir(ROOT)
    load_dotenv(ROOT / ".env", override=False)
    injected_password = False
    try:
        settings = DatabaseSettings.from_env(require_password=False)
        try:
            with socket.create_connection((settings.host, settings.port), timeout=3):
                pass
        except OSError:
            print(f"无法访问数据库通道 {settings.host}:{settings.port}。")
            print("请在另一个 PowerShell 窗口运行以下命令并保持窗口打开：")
            print("ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:15432:127.0.0.1:5432 root@47.243.206.67")
            print("SSH 窗口输入服务器密码；本程序询问的是 PostgreSQL 数据库密码。")
            return 1

        if not os.getenv("PGPASSWORD"):
            # 非交互运行不得回退到可见输入，避免密码意外回显。
            if not sys.stdin.isatty():
                print("请在交互式 PowerShell 中运行本程序，以隐藏方式输入数据库密码。")
                return 1
            os.environ["PGPASSWORD"] = getpass.getpass("PostgreSQL 数据库密码（输入不显示）：")
            injected_password = True

        report = inspect_database()
        print("数据库连接成功（只读检查，未修改数据）：")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if args.check_only:
            return 0

        import uvicorn
        print(f"本地接口文档：http://127.0.0.1:{args.port}/docs")
        print(f"数据库检查：http://127.0.0.1:{args.port}/health/database")
        print("此窗口保持打开；按 Ctrl+C 停止。")
        uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, reload=False)
        return 0
    except DatabaseCheckError as exc:
        print(f"{exc.code}：{exc.message}")
        return 1
    except (KeyboardInterrupt, EOFError):
        print("已取消或停止。")
        return 130
    finally:
        if injected_password:
            os.environ.pop("PGPASSWORD", None)


if __name__ == "__main__":
    raise SystemExit(main())
