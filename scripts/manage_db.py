import argparse
import sys
from importlib import import_module
from uuid import uuid4
from alembic import command
from alembic.config import Config
from _bootstrap import ROOT
from backend.shared.settings import MODULES, Settings

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["migrate", "seed", "revision"])
    parser.add_argument("--module", choices=MODULES)
    parser.add_argument("--message")
    args = parser.parse_args()
    if args.action == "revision" and (not args.module or not args.message):
        parser.error("revision必须提供--module与--message")
    for module in ([args.module] if args.module else MODULES):
        if args.action == "seed":
            if Settings.load(module).app_env != "test":
                raise RuntimeError("共同测试seed仅允许APP_ENV=test")
            import_module(f"backend.{module}.seed").seed()
        else:
            cfg = Config(str(ROOT / "alembic.ini"))
            cfg.set_main_option("script_location", str(ROOT / "backend" / module / "migrations"))
            if args.action == "migrate":
                command.upgrade(cfg, "head")
            else:
                command.revision(cfg, message=args.message, autogenerate=True,
                                 rev_id=f"{module}_{uuid4().hex[:12]}")
        print(f"{module}: {args.action}完成")

if __name__ == "__main__":
    try:
        main()
    except NotImplementedError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        # 数据库错误可能带连接凭据；不向共享终端输出原异常。
        print(f"操作失败({type(exc).__name__})：检查本模块实现、数据库连接与迁移，不打印凭据。", file=sys.stderr)
        sys.exit(1)
