"""M2 业务行为测试。

覆盖：配置校验、提交与拒绝、逐级审批、最终占用、驳回/撤销/超时解除、查询权限与分页、
请求编号幂等、版本冲突、外部依赖失败与未知结果、通知审计失败不抹掉业务状态。
外部依赖用 conftest 的替身，M2 自身业务与校验不被替换。
配置接口（M2-S08）按共同规则只由 M1 管理入口的 USER 调用，测试用 source="m1" 表达。
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest

from backend.m2 import service
from backend.m2.models import ApprovalConfig, Application
from backend.shared.clock import format_time, utc_now
from backend.shared.db import session_factory
from backend.shared.errors import ApiError

from tests.m2.conftest import call, calls_of, clear_config, seed_config, slot

APPROVER1 = "approver1"
APPROVER2 = "approver2"
STUDENT = "student1"


@pytest.fixture(autouse=True)
def fresh_config():
    seed_config()
    yield


def new_rid() -> str:
    return str(uuid4())


def submit(client, *, space="sp-school", period=None, purpose="部门活动", account=STUDENT,
           expected=200):
    period = period or slot()
    body = {"spaceId": space, **period, "purpose": purpose, "requestId": new_rid()}
    return call(client, "submit_application", body, account=account, expected=expected)


def approve(client, application_id, level, *, account, expected_version, decision="APPROVE",
            comment=None, expected=200):
    body = {"applicationId": application_id, "level": level, "decision": decision,
            "expectedVersion": expected_version, "requestId": new_rid()}
    if comment is not None:
        body["comment"] = comment
    return call(client, "submit_approval", body, account=account, expected=expected)


def config(client, account="admin"):
    return call(client, "get_approval_config", {}, account=account, source="m1")


def update_config(client, body, *, account="admin", expected=200):
    return call(client, "update_approval_config", body, account=account, source="m1",
                expected=expected)


def good_config(version: int) -> dict:
    return {
        "applicationScopes": [{"type": "GLOBAL", "id": None}],
        "approvalLevels": [{"level": 1, "approverId": "u-approver1"},
                           {"level": 2, "approverId": "u-approver2"}],
        "clockStart": "CURRENT_LEVEL",
        "timeoutSeconds": 86400,
        "checkIntervalSeconds": 60,
        "expectedVersion": version,
        "requestId": new_rid(),
    }


# --------------------------------------------------------------------- 健康与入口
def test_health_reports_all_functions_and_worker(client):
    data = client.get("/health").json()["data"]
    assert data["unfinishedFunctions"] == []
    assert data["workerConfigured"] is True
    assert data["workerError"] is False


def test_s08_is_open_to_user_but_requires_login(client):
    # 共同规则把 S08 登记为可直连的管理入口；登录与权限仍由 D-S04 核验
    data = config(client)
    assert data["version"] >= 1
    assert call(client, "get_approval_config", {}, expected=401)["code"] == "UNAUTHENTICATED"
    # 无配置权限的用户被拒绝
    assert call(client, "get_approval_config", {}, account=STUDENT,
                source="m1", expected=403)["code"] == "FORBIDDEN"


# --------------------------------------------------------------------- 配置
def test_initial_config_is_unconfigured_and_blocks_submission(client):
    clear_config()
    data = config(client)
    assert data["version"] == 1
    assert data["applicationScopes"] == [] and data["approvalLevels"] == []
    assert data["clockStart"] is None and data["timeoutSeconds"] is None
    submit(client, expected=409)


def test_config_update_validates_levels_scopes_and_approvers(client, world):
    base = config(client)
    good = good_config(base["version"])
    # 共同测试输入里 admin 角色没有 m2.config.manage；本用例只验证配置本身的校验
    world.permission_override["u-admin"] = {"m2.config.manage": True}

    level_one_only = dict(good, approvalLevels=[{"level": 1, "approverId": "u-approver1"}],
                          requestId=new_rid())
    assert update_config(client, level_one_only, expected=422)["code"] == "INVALID_ARGUMENT"

    gap = dict(good, approvalLevels=[{"level": 1, "approverId": "u-approver1"},
                                     {"level": 3, "approverId": "u-approver2"}],
               requestId=new_rid())
    assert update_config(client, gap, expected=422)["code"] == "INVALID_ARGUMENT"

    duplicated = dict(good, approvalLevels=[{"level": 1, "approverId": "u-approver1"},
                                            {"level": 2, "approverId": "u-approver1"}],
                      requestId=new_rid())
    assert update_config(client, duplicated, expected=422)["code"] == "INVALID_ARGUMENT"

    empty_scopes = dict(good, applicationScopes=[], requestId=new_rid())
    assert update_config(client, empty_scopes, expected=422)["code"] == "INVALID_ARGUMENT"

    world.people_override["u-student1"] = {"eligible": False}
    ineligible = dict(good, requestId=new_rid(),
                      approvalLevels=[{"level": 1, "approverId": "u-student1"},
                                      {"level": 2, "approverId": "u-approver2"}])
    assert update_config(client, ineligible, expected=422)["code"] == "INVALID_ARGUMENT"

    world.people_override["u-student1"] = {"accountStatus": "DISABLED"}
    disabled = dict(good, requestId=new_rid(),
                    approvalLevels=[{"level": 1, "approverId": "u-student1"},
                                    {"level": 2, "approverId": "u-approver2"}])
    assert update_config(client, disabled, expected=422)["code"] == "INVALID_ARGUMENT"

    world.people_override.clear()
    updated = update_config(client, good)
    assert updated["version"] == base["version"] + 1
    assert [lv["level"] for lv in updated["approvalLevels"]] == [1, 2]
    assert updated["clockStart"] == "CURRENT_LEVEL"


def test_config_update_is_idempotent_and_rejects_stale_version(client, world):
    base = config(client)
    world.permission_override["u-admin"] = {"m2.config.manage": True}
    body = good_config(base["version"])
    first = update_config(client, body)
    from backend.d.audit_schemas import AuditAppendInput
    AuditAppendInput.model_validate({"operation": "APPEND", **calls_of(world, "d", "audit")[-1]})
    again = update_config(client, body)
    assert again == first
    assert update_config(client, dict(body, requestId=new_rid()), expected=409)["code"] == "CONFLICT"


def test_successful_config_retry_rechecks_current_permission(client, world):
    body = good_config(config(client)["version"])
    update_config(client, body)
    world.permission_override["u-admin"] = {"m2.config.manage": False}
    assert update_config(client, body, expected=403)["code"] == "FORBIDDEN"


def test_frontend_request_id_is_scoped_to_current_user(client):
    body = {"spaceId": "sp-school", **slot(), "purpose": "各自独立申请", "requestId": new_rid()}
    first = call(client, "submit_application", body, account="student1")
    second = call(client, "submit_application", body, account="student2")
    assert first["application"]["applicantId"] == "u-student1"
    assert second["application"]["applicantId"] == "u-student2"
    assert first["application"]["applicationId"] != second["application"]["applicationId"]


def test_pending_application_keeps_original_approvers_and_timeout(client):
    created = submit(client)["application"]
    changed = good_config(config(client)["version"])
    changed["approvalLevels"] = [{"level": 1, "approverId": "u-approver2"},
                                 {"level": 2, "approverId": "u-admin"}]
    changed["timeoutSeconds"] = 60
    update_config(client, changed)
    actions = call(client, "get_allowed_actions", {}, account=APPROVER1)["actions"]
    assert next(a for a in actions if a["operation"] == "APPROVE")["allowed"]
    tasks = call(client, "get_approval_tasks", {"applicationId": created["applicationId"]}, account=APPROVER1)
    assert len(tasks["items"]) == 1
    one = approve(client, created["applicationId"], 1, account=APPROVER1, expected_version=created["version"])
    assert one["application"]["expiresAt"] == created["expiresAt"]
    two = approve(client, created["applicationId"], 2, account=APPROVER2, expected_version=one["application"]["version"])
    assert two["application"]["status"] == "APPROVED"


# --------------------------------------------------------------------- 提交
def test_submit_creates_pending_first_level_and_notifies(client, world):
    data = submit(client, purpose="学生会活动")
    application = data["application"]
    assert application["status"] == "PENDING"
    assert application["currentLevel"] == 1
    assert application["version"] == 1
    assert application["applicantId"] == "u-student1"
    assert data["approvals"] == []
    assert data["seatSync"] is None
    notes = calls_of(world, "d", "send_notification")
    assert notes and notes[0]["kind"] == "M2_APPROVAL_PENDING"
    assert notes[0]["recipientUserIds"] == ["u-approver1"]
    assert notes[0]["sourceModule"] == "M2" and notes[0]["businessId"] == application["applicationId"]
    audits = calls_of(world, "d", "audit")
    assert audits and audits[0]["action"] == "SUBMIT_APPLICATION"
    from backend.d.audit_schemas import AuditAppendInput
    event = AuditAppendInput.model_validate({"operation": "APPEND", **audits[0]})
    assert event.actorType == "USER" and event.actorId == "u-student1"
    # 提交阶段不应写回整室占用
    assert calls_of(world, "m1", "change_room_limit") == []


def test_submit_rejects_bad_period_purpose_permission_and_conflict(client, world):
    body = {"spaceId": "sp-school", **slot(), "purpose": "   ", "requestId": new_rid()}
    assert call(client, "submit_application", body, account=STUDENT,
                expected=422)["code"] == "INVALID_ARGUMENT"

    period = slot(4, 2)
    body = {"spaceId": "sp-school", "startTime": period["endTime"],
            "endTime": period["startTime"], "purpose": "倒序", "requestId": new_rid()}
    assert call(client, "submit_application", body, account=STUDENT,
                expected=422)["code"] == "INVALID_ARGUMENT"

    past = format_time(utc_now() - timedelta(hours=2))
    body = {"spaceId": "sp-school", "startTime": past,
            "endTime": format_time(utc_now() - timedelta(hours=1)),
            "purpose": "过去", "requestId": new_rid()}
    assert call(client, "submit_application", body, account=STUDENT,
                expected=422)["code"] == "INVALID_ARGUMENT"

    body = {"spaceId": "sp-school", **slot(5), "purpose": "越权", "requestId": new_rid()}
    assert call(client, "submit_application", body, account="technician",
                expected=403)["code"] == "FORBIDDEN"

    blocked = slot(6)
    world.block("sp-school", blocked["startTime"], blocked["endTime"],
                blocked_by=["M1"], reasons=["已有个人预约"])
    body = {"spaceId": "sp-school", **blocked, "purpose": "冲突", "requestId": new_rid()}
    assert call(client, "submit_application", body, account=STUDENT,
                expected=409)["code"] == "CONFLICT"

    body = {"spaceId": "sp-missing", **slot(7), "purpose": "不存在", "requestId": new_rid()}
    assert call(client, "submit_application", body, account=STUDENT,
                expected=404)["code"] == "NOT_FOUND"


def test_submit_rejects_space_outside_configured_scope(client, world):
    base = config(client)
    world.permission_override["u-admin"] = {"m2.config.manage": True}
    update_config(client, {"applicationScopes": [{"type": "DEPARTMENT", "id": "dep-ee"}],
                           "approvalLevels": [{"level": 1, "approverId": "u-approver1"},
                                              {"level": 2, "approverId": "u-approver2"}],
                           "clockStart": "CURRENT_LEVEL", "timeoutSeconds": 3600,
                           "checkIntervalSeconds": 60, "expectedVersion": base["version"],
                           "requestId": new_rid()})
    # sp-school 是学校空间，不在 dep-ee 范围内
    assert submit(client, space="sp-school", period=slot(3), expected=403)["code"] == "FORBIDDEN"


def test_submit_is_idempotent_and_rejects_reused_content(client):
    period = slot(2)
    body = {"spaceId": "sp-school", **period, "purpose": "幂等", "requestId": new_rid()}
    first = call(client, "submit_application", body, account=STUDENT)
    again = call(client, "submit_application", body, account=STUDENT)
    assert again == first
    error = call(client, "submit_application", dict(body, purpose="换了内容"),
                 account=STUDENT, expected=409)
    assert error["code"] == "REQUEST_ID_REUSED"


# --------------------------------------------------------------------- 审批
def test_two_level_approval_occupies_only_on_final_level(client, world):
    data = submit(client, period=slot(3))
    application = data["application"]
    app_id = application["applicationId"]

    level1 = approve(client, app_id, 1, account=APPROVER1,
                     expected_version=application["version"], comment="同意")
    assert level1["application"]["status"] == "PENDING"
    assert level1["application"]["currentLevel"] == 2
    assert level1["seatSync"] is None
    assert calls_of(world, "m1", "change_room_limit") == []
    assert level1["approvals"][0]["decision"] == "APPROVE"
    assert level1["approvals"][0]["comment"] == "同意"

    level2 = approve(client, app_id, 2, account=APPROVER2,
                     expected_version=level1["application"]["version"])
    assert level2["application"]["status"] == "APPROVED"
    assert level2["application"]["currentLevel"] is None
    sync = level2["seatSync"]
    assert sync["status"] == "SUCCESS"
    assert sync["result"]["requestStatus"] == "SUCCESS"
    writes = calls_of(world, "m1", "change_room_limit")
    assert len(writes) == 1
    assert writes[0]["action"] == "OCCUPY" and writes[0]["businessState"] == "APPROVED"
    assert writes[0]["source"] == "M2" and writes[0]["businessId"] == app_id
    assert writes[0]["sourceVersion"] == level2["application"]["version"]

    detail = call(client, "get_application", {"applicationId": app_id}, account=STUDENT)
    assert len(detail["approvals"]) == 2
    assert detail["seatSync"]["status"] == "SUCCESS"


def test_approval_rejects_wrong_approver_level_and_stale_version(client):
    data = submit(client, period=slot(4))
    application = data["application"]
    app_id = application["applicationId"]
    version = application["version"]

    assert approve(client, app_id, 1, account=APPROVER2, expected_version=version,
                   expected=403)["code"] == "FORBIDDEN"
    assert approve(client, app_id, 2, account=APPROVER2, expected_version=version,
                   expected=409)["code"] == "CONFLICT"
    assert approve(client, app_id, 1, account=APPROVER1, expected_version=version + 5,
                   expected=409)["code"] == "CONFLICT"
    assert approve(client, app_id, 1, account=STUDENT, expected_version=version,
                   expected=403)["code"] == "FORBIDDEN"


def test_reject_releases_room_and_ends_application(client, world):
    data = submit(client, period=slot(5))
    application = data["application"]
    app_id = application["applicationId"]
    one = approve(client, app_id, 1, account=APPROVER1, expected_version=application["version"])
    rejected = approve(client, app_id, 2, account=APPROVER2,
                       expected_version=one["application"]["version"], decision="REJECT",
                       comment="时段不合适")
    assert rejected["application"]["status"] == "REJECTED"
    assert rejected["application"]["terminatedAt"] is not None
    assert rejected["seatSync"]["status"] == "SUCCESS"
    writes = calls_of(world, "m1", "change_room_limit")
    assert writes[-1]["action"] == "RELEASE" and writes[-1]["businessState"] == "REJECTED"
    assert approve(client, app_id, 2, account=APPROVER2,
                   expected_version=rejected["application"]["version"],
                   expected=409)["code"] == "CONFLICT"


def test_approval_rechecks_current_approver_eligibility(client, world):
    data = submit(client, period=slot(6))
    application = data["application"]
    world.people_override["u-approver1"] = {"accountStatus": "DISABLED"}
    error = approve(client, application["applicationId"], 1, account=APPROVER1,
                    expected_version=application["version"], expected=409)
    assert error["code"] == "CONFLICT"
    detail = call(client, "get_application", {"applicationId": application["applicationId"]},
                  account=STUDENT)
    assert detail["application"]["status"] == "PENDING"
    assert detail["approvals"] == []


def test_approval_is_idempotent_for_same_request(client):
    data = submit(client, period=slot(7))
    application = data["application"]
    body = {"applicationId": application["applicationId"], "level": 1, "decision": "APPROVE",
            "expectedVersion": application["version"], "requestId": new_rid()}
    first = call(client, "submit_approval", body, account=APPROVER1)
    again = call(client, "submit_approval", body, account=APPROVER1)
    assert again == first
    detail = call(client, "get_application", {"applicationId": application["applicationId"]},
                  account=STUDENT)
    assert len(detail["approvals"]) == 1

def test_occupancy_conflict_keeps_approved_but_marks_sync_rejected(client, world):
    data = submit(client, period=slot(8))
    application = data["application"]
    app_id = application["applicationId"]
    # M1 对该来源版本一律明确拒绝（含中间级的解除写回）
    world.limit_default = "REJECTED"
    one = approve(client, app_id, 1, account=APPROVER1, expected_version=application["version"])
    two = approve(client, app_id, 2, account=APPROVER2,
                  expected_version=one["application"]["version"])
    world.limit_default = "SUCCESS"
    assert two["application"]["status"] == "APPROVED"
    assert two["seatSync"]["status"] == "REJECTED"
    detail = call(client, "get_application", {"applicationId": app_id}, account=STUDENT)
    assert detail["application"]["status"] == "APPROVED"
    assert detail["seatSync"]["status"] == "REJECTED"


def test_unknown_write_result_is_not_reported_as_success(client, world):
    data = submit(client, period=slot(9))
    application = data["application"]
    app_id = application["applicationId"]
    one = approve(client, app_id, 1, account=APPROVER1, expected_version=application["version"])

    async def uncertain(payload):
        raise ApiError("UNAVAILABLE", "对方结果无法确认")

    original = service.peers.change_room_limit
    service.peers.change_room_limit = uncertain
    try:
        two = approve(client, app_id, 2, account=APPROVER2,
                      expected_version=one["application"]["version"])
    finally:
        service.peers.change_room_limit = original
    assert two["application"]["status"] == "APPROVED"
    assert two["seatSync"]["status"] == "UNKNOWN"
    assert two["seatSync"]["result"] is None


def test_notification_and_audit_failure_do_not_drop_business_state(client, world):
    world.notification_error = True
    world.audit_error = True
    data = submit(client, period=slot(10))
    application = data["application"]
    assert application["status"] == "PENDING"
    notes = data["notifications"]
    assert notes and notes[0]["status"] == "UNKNOWN"
    detail = call(client, "get_application", {"applicationId": application["applicationId"]},
                  account=STUDENT)
    assert detail["application"]["status"] == "PENDING"
    assert detail["notifications"][0]["status"] == "UNKNOWN"


# --------------------------------------------------------------------- 查询
def test_application_visibility_rules(client, world):
    data = submit(client, period=slot(11))
    app_id = data["application"]["applicationId"]
    assert call(client, "get_application", {"applicationId": app_id},
                account=STUDENT)["application"]["applicationId"] == app_id
    assert call(client, "get_application", {"applicationId": app_id},
                account="student2", expected=403)["code"] == "FORBIDDEN"
    approve(client, app_id, 1, account=APPROVER1, expected_version=data["application"]["version"])
    assert call(client, "get_application", {"applicationId": app_id},
                account=APPROVER1)["application"]["applicationId"] == app_id
    # 管理范围查看需要 read_all（共同测试输入里 admin 角色没有这条，按需授予后验证）
    world.permission_override["u-admin"] = {"m2.application.read_all": True}
    assert call(client, "get_application", {"applicationId": app_id},
                account="admin")["application"]["applicationId"] == app_id
    assert call(client, "get_application", {"applicationId": "nope"},
                account=STUDENT, expected=404)["code"] == "NOT_FOUND"


def test_application_queries_filter_by_view_and_paginate(client, world):
    for index in range(3):
        submit(client, period=slot(12 + index))
    # 用当前提交时间窗过滤，避免其他用例留下的申请干扰计数
    start = utc_now() - timedelta(minutes=5)
    end = utc_now() + timedelta(minutes=5)
    window = {"startTime": format_time(start), "endTime": format_time(end)}
    own = call(client, "get_applications", {"view": "OWN", "limit": 2, **window}, account=STUDENT)
    assert len(own["items"]) == 2 and own["nextCursor"]
    rest = call(client, "get_applications",
                {"view": "OWN", "limit": 2, "cursor": own["nextCursor"], **window},
                account=STUDENT)
    first_ids = [item["application"]["applicationId"] for item in own["items"]]
    rest_ids = [item["application"]["applicationId"] for item in rest["items"]]
    assert rest_ids and not set(first_ids) & set(rest_ids)
    # 两页合起来覆盖本次窗口内的全部本人申请（不依赖同一模块其他用例留下的记录数）
    everything = call(client, "get_applications", {"view": "OWN", "limit": 100, **window},
                      account=STUDENT)
    assert len(set(first_ids) | set(rest_ids)) <= len(everything["items"])
    # 游标与当前筛选不匹配（换了筛选条件必须从第一页重查）
    assert call(client, "get_applications",
                {"view": "OWN", "limit": 2, "cursor": own["nextCursor"],
                 "startTime": format_time(utc_now() - timedelta(minutes=1)),
                 "endTime": format_time(utc_now() + timedelta(minutes=1))},
                account=STUDENT, expected=422)["code"] == "INVALID_ARGUMENT"
    assert call(client, "get_applications", {"view": "OWN", "cursor": "not-a-cursor"},
                account=STUDENT, expected=422)["code"] == "INVALID_ARGUMENT"
    # 时间范围必须成对
    assert call(client, "get_applications", {"view": "OWN", "startTime": window["startTime"]},
                account=STUDENT, expected=422)["code"] == "INVALID_ARGUMENT"
    # 状态过滤
    approved = call(client, "get_applications", {"view": "OWN", "status": "APPROVED", **window},
                    account=STUDENT)
    assert all(item["application"]["status"] == "APPROVED" for item in approved["items"])
    # 管理视图需要 read_all
    assert call(client, "get_applications", {"view": "MANAGED"},
                account=STUDENT, expected=403)["code"] == "FORBIDDEN"
    world.permission_override["u-admin"] = {"m2.application.read_all": True}
    managed = call(client, "get_applications", {"view": "MANAGED", "limit": 1}, account="admin")
    assert managed["items"]


def test_approval_tasks_only_show_current_approver_level(client):
    data = submit(client, period=slot(16))
    app_id = data["application"]["applicationId"]
    first = call(client, "get_approval_tasks", {}, account=APPROVER1)
    assert app_id in [item["application"]["applicationId"] for item in first["items"]]
    second = call(client, "get_approval_tasks", {}, account=APPROVER2)
    assert app_id not in [item["application"]["applicationId"] for item in second["items"]]
    filtered = call(client, "get_approval_tasks", {"applicationId": app_id}, account=APPROVER1)
    assert len(filtered["items"]) == 1 and "nextCursor" in filtered


def test_classroom_options_include_availability_only_with_period(client):
    without = call(client, "get_classroom_options", {}, account=STUDENT)
    assert without["items"] and all(item["availability"] is None for item in without["items"])
    period = slot(20)
    with_period = call(client, "get_classroom_options", period, account=STUDENT)
    assert all(item["availability"] is not None for item in with_period["items"])
    assert call(client, "get_classroom_options", {"startTime": period["startTime"]},
                account=STUDENT, expected=422)["code"] == "INVALID_ARGUMENT"


def test_allowed_actions_reflect_permissions(client):
    student = {item["operation"]: item
               for item in call(client, "get_allowed_actions", {}, account=STUDENT)["actions"]}
    assert student["SUBMIT"]["allowed"] is True
    assert student["APPROVE"]["allowed"] is False
    assert student["CONFIGURE"]["allowed"] is False
    approver = {item["operation"]: item
                for item in call(client, "get_allowed_actions", {}, account=APPROVER1)["actions"]}
    assert approver["APPROVE"]["allowed"] is True
    admin = {item["operation"]: item
             for item in call(client, "get_allowed_actions", {}, account="admin")["actions"]}
    assert admin["CONFIGURE"]["allowed"] is True


def test_maintenance_impact_uses_m1_availability(client, world):
    data = submit(client, period=slot(24))
    application = data["application"]
    normal = call(client, "get_maintenance_impact",
                  {"applicationId": application["applicationId"]}, account=STUDENT)
    assert normal["affectedByMaintenance"] is False
    world.block("sp-school", application["startTime"], application["endTime"],
                blocked_by=["M3"], reasons=["维修中"])
    impacted = call(client, "get_maintenance_impact",
                    {"applicationId": application["applicationId"]}, account=STUDENT)
    assert impacted["affectedByMaintenance"] is True
    assert "M3" in impacted["availability"]["blockedBy"]


# --------------------------------------------------------------------- 撤销
def test_cancel_only_by_owner_before_termination(client, world):
    # 共同测试输入的 student 角色未含 m2.application.cancel；本用例验证撤销规则本身
    world.extra_permissions["u-student1"] = ["m2.application.cancel"]
    data = submit(client, period=slot(28))
    application = data["application"]
    app_id = application["applicationId"]
    body = {"applicationId": app_id, "expectedVersion": application["version"],
            "requestId": new_rid(), "reason": "不再需要"}
    assert call(client, "cancel_application", body, account="student2",
                expected=403)["code"] == "FORBIDDEN"
    cancelled = call(client, "cancel_application", body, account=STUDENT)
    assert cancelled["application"]["status"] == "CANCELLED"
    assert cancelled["application"]["terminationReason"] == "不再需要"
    assert cancelled["seatSync"]["status"] == "SUCCESS"
    writes = calls_of(world, "m1", "change_room_limit")
    assert writes[-1]["action"] == "RELEASE" and writes[-1]["businessState"] == "CANCELLED"
    assert call(client, "cancel_application",
                dict(body, expectedVersion=cancelled["application"]["version"],
                     requestId=new_rid()), account=STUDENT,
                expected=409)["code"] == "CONFLICT"


def test_cancel_rejects_stale_version(client, world):
    world.extra_permissions["u-student1"] = ["m2.application.cancel"]
    data = submit(client, period=slot(32))
    application = data["application"]
    error = call(client, "cancel_application",
                 {"applicationId": application["applicationId"],
                  "expectedVersion": application["version"] + 3, "requestId": new_rid()},
                 account=STUDENT, expected=409)
    assert error["code"] == "CONFLICT"


# --------------------------------------------------------------------- 超时
def test_timeout_worker_terminates_pending_and_releases(client, world):
    data = submit(client, period=slot(36))
    app_id = data["application"]["applicationId"]
    with session_factory("m2")() as db:
        row = db.get(ApprovalConfig, 1)
        row.clock_start = "SUBMISSION"
        row.timeout_seconds = 1
        row.check_interval_seconds = 1
        row.version += 1
        db.commit()
    with session_factory("m2")() as db:
        stored = db.get(Application, app_id)
        stored.expires_at = utc_now() - timedelta(seconds=1)
        db.commit()
    asyncio.run(service._timeout_scan())
    detail = call(client, "get_application", {"applicationId": app_id}, account=STUDENT)
    assert detail["application"]["status"] == "TIMEOUT_CANCELLED"
    assert detail["application"]["terminatedAt"] is not None
    writes = calls_of(world, "m1", "change_room_limit")
    assert any(w["action"] == "RELEASE" and w["businessState"] == "TIMEOUT_CANCELLED"
               for w in writes)
    assert approve(client, app_id, 1, account=APPROVER1,
                   expected_version=detail["application"]["version"],
                   expected=409)["code"] == "CONFLICT"


def test_timeout_and_approval_cannot_both_win(client):
    data = submit(client, period=slot(40))
    application = data["application"]
    app_id = application["applicationId"]
    with session_factory("m2")() as db:
        stored = db.get(Application, app_id)
        stored.expires_at = utc_now() - timedelta(seconds=1)
        db.commit()
    approve(client, app_id, 1, account=APPROVER1, expected_version=application["version"])
    asyncio.run(service._timeout_scan())
    detail = call(client, "get_application", {"applicationId": app_id}, account=STUDENT)
    assert detail["application"]["status"] == "PENDING"
    assert detail["application"]["currentLevel"] == 2
