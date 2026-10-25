"""真实D授权数据支持本轮补充功能，普通用户仍不能管理他人记录。"""
import backend.d.seed as seed_module
from backend.d.permissions import check_user_permission_in_session


def test_seed_grants_included_features_but_keeps_management_boundary(isolated_db):
    seed_module.seed()
    cases = [
        ("u-student1", "m2.application.cancel", True),
        ("u-student1", "m3.work_order.read", True),
        ("u-student1", "m3.work_order.cancel", True),
        ("u-student1", "m2.application.read_all", False),
        ("u-student1", "m3.work_order.read_all", False),
        ("u-student1", "m3.statistics.read", False),
        ("u-technician", "m3.work_order.read", True),
        ("u-admin", "m2.application.read_all", True),
        ("u-admin", "m3.work_order.read_all", True),
        ("u-admin", "m3.statistics.read", True),
    ]
    with isolated_db() as db:
        for user_id, code, allowed in cases:
            result = check_user_permission_in_session(db, permission_code=code,
                target_scope={"type": "SPACE", "id": "sp-school"}, user_id=user_id)
            assert result["allowed"] is allowed, (user_id, code)
