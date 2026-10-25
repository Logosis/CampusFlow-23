"""共同测试账号应能使用本轮功能，并保留普通用户与管理员的边界。"""
import json
from pathlib import Path


def test_initial_roles_can_use_included_features_without_admin_privileges():
    fixture = json.loads((Path(__file__).resolve().parents[1] / "fixtures/bootstrap.json").read_text(encoding="utf-8"))
    roles = fixture["roles"]
    student = set(roles["student"]["permissionCodes"])
    assert {"m2.application.cancel", "m3.work_order.read", "m3.work_order.cancel"} <= student
    assert {"m2.application.read_all", "m3.work_order.read_all", "m3.statistics.read", "d.rbac.manage"}.isdisjoint(student)
    admin = set(roles["admin"]["permissionCodes"])
    assert {"m2.application.cancel", "m2.application.read_all", "m2.config.manage",
            "m3.work_order.cancel", "m3.work_order.read_all", "m3.statistics.read"} <= admin
    assert "m3.work_order.read" in roles["technician"]["permissionCodes"]
    for role in roles.values():
        assert len(role["permissionCodes"]) == len(set(role["permissionCodes"]))
        assert set(role["permissionCodes"]) <= set(fixture["permissions"])
