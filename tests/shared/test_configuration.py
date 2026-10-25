"""新环境可登录；补全旧配置不能更换数据库密码或已有会话密钥。"""
from pathlib import Path
import re
import runpy
import sys

from dotenv import dotenv_values
import pytest

ROOT = Path(__file__).resolve().parents[2]


def configure(tmp_path, monkeypatch, *arguments):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    namespace = runpy.run_path(str(ROOT / "scripts" / "configure.py"))
    main = namespace["main"]
    monkeypatch.setitem(main.__globals__, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["configure.py", *arguments])
    return main()


def test_new_configuration_generates_secret_without_printing_it(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env.example").write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    configure(tmp_path, monkeypatch, "--test")
    values = dotenv_values(tmp_path / ".env")
    assert re.fullmatch(r"[0-9a-f]{64}", values["D_SESSION_SECRET"])
    assert values["APP_ENV"] == "test"
    assert values["D_SESSION_SECRET"] not in capsys.readouterr().out


def test_complete_preserves_existing_configuration_and_secret(tmp_path, monkeypatch, capsys):
    path = tmp_path / ".env"
    previous = "APP_ENV=test\nD_DB_PASSWORD=existing-password\nD_SESSION_SECRET=\n"
    path.write_text(previous, encoding="utf-8")
    configure(tmp_path, monkeypatch, "--complete")
    first = path.read_bytes()
    secret = dotenv_values(path)["D_SESSION_SECRET"]
    assert re.fullmatch(r"[0-9a-f]{64}", secret)
    assert path.read_text(encoding="utf-8").startswith(previous)
    assert dotenv_values(path)["D_DB_PASSWORD"] == "existing-password"
    configure(tmp_path, monkeypatch, "--complete")
    assert path.read_bytes() == first
    assert secret not in capsys.readouterr().out


def test_invalid_existing_secret_is_not_replaced(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    previous = "D_SESSION_SECRET=invalid\nD_DB_PASSWORD=keep\n"
    path.write_text(previous, encoding="utf-8")
    with pytest.raises(SystemExit):
        configure(tmp_path, monkeypatch, "--complete")
    assert path.read_text(encoding="utf-8") == previous
