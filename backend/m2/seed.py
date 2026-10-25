"""M2 测试初始化：按共同测试输入写入本模块自己的配置。

输入见 tests/fixtures/bootstrap.json 的 m2 段（tests/fixtures/bootstrap.json 只用于隔离测试）。
可重复运行：已存在的配置不重建、不覆盖测试运行中的调整；零个审批级别等未配置状态不会被写入假数据。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from sqlalchemy import select

from backend.m2.models import ApprovalConfig
from backend.shared.db import session_factory


def _fixture() -> dict:
    root = Path(__file__).resolve().parents[2]
    data = json.loads((root / "tests/fixtures/bootstrap.json").read_text(encoding="utf-8"))
    return data.get("m2") or {}


def seed() -> None:
    if os.getenv("APP_ENV") != "test":
        raise RuntimeError("M2 seed 只用于 APP_ENV=test")
    fixture = _fixture()
    if not fixture:
        raise RuntimeError("共同测试输入缺少 m2 段")
    with session_factory("m2")() as db:
        existing = db.get(ApprovalConfig, 1)
        if existing is not None and (existing.approval_levels or existing.application_scopes):
            # 已有配置不覆盖：保留测试运行中的调整
            return
        row = existing or ApprovalConfig(id=1)
        row.version = int(fixture.get("version", 1))
        row.application_scopes = list(fixture.get("applicationScopes") or [])
        row.approval_levels = list(fixture.get("approvalLevels") or [])
        row.clock_start = fixture.get("clockStart")
        row.timeout_seconds = fixture.get("timeoutSeconds")
        row.check_interval_seconds = fixture.get("checkIntervalSeconds")
        if existing is None:
            db.add(row)
        db.commit()
