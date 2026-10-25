"""通知数据准备：严格契约、真实 PostgreSQL 约束与保留已有数据的迁移。"""

from datetime import datetime, timedelta, timezone
import re

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from pydantic import ValidationError
import pytest
from sqlalchemy import delete, func, inspect, select, text
from sqlalchemy.exc import IntegrityError

import backend.d.seed as seed_module
from backend.d.models import Base, Notification, NotificationDelivery, PublicConfig, Space, User
from backend.d.notification_schemas import (
    DeliveryOutput, GetNotificationInput, KINDS_BY_SOURCE, NotificationOutput, SendNotificationInput,
)
from backend.shared.clock import format_time
from backend.shared.settings import ROOT


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
NEW_TABLES = {"notifications", "notification_deliveries"}


def payload(**changes):
    return {
        "requestId": "notify-request-1", "sourceModule": "M3", "businessId": "work-order-test",
        "spaceId": "sp-school", "recipientUserIds": ["u-student1", "u-technician"],
        "kind": "M3_ASSIGNED", "title": "维修派单测试", "body": "请检查指定教室。\n这是测试正文。", **changes,
    }


@pytest.mark.parametrize("source,kind", [(s, k) for s, kinds in KINDS_BY_SOURCE.items() for k in kinds])
def test_all_fifteen_documented_notification_kinds(source, kind):
    data = payload(sourceModule=source, kind=kind)
    assert SendNotificationInput.model_validate(data).model_dump() == data


@pytest.mark.parametrize("changes", [
    {"sourceModule": "m3"}, {"sourceModule": "D"}, {"sourceModule": None},
    {"kind": "M2_APPROVED"}, {"kind": "OTHER"}, {"kind": None},
    {"requestId": ""}, {"requestId": " \t"}, {"requestId": 12},
    {"businessId": " "}, {"businessId": None}, {"spaceId": " "}, {"spaceId": 12},
    {"recipientUserIds": []}, {"recipientUserIds": ["u-student1", "u-student1"]},
    {"recipientUserIds": [" "]}, {"recipientUserIds": [None]}, {"recipientUserIds": "u-student1"},
    {"recipientUserIds": [12]}, {"recipientUserIds": None},
    {"title": ""}, {"title": " \t"}, {"title": "x\ny"}, {"title": "x\ry"}, {"title": "x" * 2001},
    {"body": ""}, {"body": "\n\t "}, {"body": "x" * 2001}, {"body": None},
    {"recipientEmails": ["example@example.test"]}, {"userId": "u-admin"}, {"retryLimit": 5},
    {"notificationId": "self-issued"}, {"channel": "EMAIL"},
])
def test_send_input_rejects_invalid_or_injected_fields(changes):
    with pytest.raises(ValidationError):
        SendNotificationInput.model_validate(payload(**changes))


@pytest.mark.parametrize("field", ["requestId", "sourceModule", "businessId", "spaceId", "recipientUserIds", "kind", "title", "body"])
def test_send_fields_are_all_required(field):
    data = payload()
    del data[field]
    with pytest.raises(ValidationError):
        SendNotificationInput.model_validate(data)


def test_send_allows_null_space_preserves_order_and_text_boundaries():
    data = payload(spaceId=None, recipientUserIds=["u-technician", "u-student1"], title="中" * 2000, body="文" * 2000)
    assert SendNotificationInput.model_validate(data).model_dump() == data


@pytest.mark.parametrize("data", [
    {}, {"notificationId": "n", "requestId": "r"}, {"notificationId": None}, {"requestId": None},
    {"notificationId": "n", "requestId": None}, {"requestId": "r", "notificationId": None},
    {"notificationId": " "}, {"requestId": ""}, {"notificationId": 1}, {"requestId": "r", "sourceModule": "M1"},
])
def test_get_requires_exactly_one_valid_identifier(data):
    with pytest.raises(ValidationError):
        GetNotificationInput.model_validate(data)


@pytest.mark.parametrize("field", ["notificationId", "requestId"])
def test_get_accepts_each_identifier_alone(field):
    result = GetNotificationInput.model_validate({field: "identifier"})
    assert getattr(result, field) == "identifier"


def public_delivery(status="PENDING"):
    attempted = status in {"SENDING", "SUBMITTED", "UNKNOWN"}
    return {"recipientUserId": "u-student1", "status": status, "attemptCount": 1 if attempted else 0,
            "lastAttemptAt": format_time(NOW) if attempted else None, "reason": "TEST_REASON"}


@pytest.mark.parametrize("status", ["PENDING", "SENDING", "SUBMITTED", "FAILED", "UNKNOWN"])
def test_output_has_exact_delivery_fields_for_each_state(status):
    data = {key: value for key, value in payload().items() if key != "recipientUserIds"}
    data.update(notificationId="n-test", channel="EMAIL", createdAt=format_time(NOW), deliveries=[public_delivery(status)])
    assert NotificationOutput.model_validate(data).model_dump() == data


def test_output_has_no_empty_successful_recipient_list():
    data = {key: value for key, value in payload().items() if key != "recipientUserIds"}
    data.update(notificationId="n-test", channel="EMAIL", createdAt=format_time(NOW), deliveries=[])
    with pytest.raises(ValidationError):
        NotificationOutput.model_validate(data)


@pytest.mark.parametrize("field", ["recipientEmail", "attemptToken", "leaseExpiresAt", "nextAttemptAt", "retryLimit", "password"])
def test_delivery_output_rejects_internal_fields(field):
    with pytest.raises(ValidationError):
        DeliveryOutput.model_validate({**public_delivery(), field: "not-public"})


@pytest.mark.parametrize("field", ["recipientUserId", "status", "attemptCount", "lastAttemptAt", "reason"])
def test_delivery_output_requires_complete_fields(field):
    data = public_delivery()
    del data[field]
    with pytest.raises(ValidationError):
        DeliveryOutput.model_validate(data)


@pytest.fixture
def notification_db(isolated_db):
    seed_module.seed()
    return isolated_db


def notice(**changes):
    return Notification(**({
        "notification_id": "n-test", "source_module": "M3", "request_id": "notify-request-1",
        "business_id": "work-order-test", "space_id": "sp-school", "kind": "M3_ASSIGNED", "channel": "EMAIL",
        "title": "维修派单测试", "body": "请检查指定教室。", "created_at": NOW, "retry_limit": 2,
        "input_data": payload(), **changes,
    }))


def delivery(**changes):
    return NotificationDelivery(**({
        "notification_id": "n-test", "recipient_user_id": "u-student1", "recipient_order": 0,
        "recipient_email": "student1@example.test", "status": "PENDING", "attempt_count": 0,
        "last_attempt_at": None, "next_attempt_at": NOW, "attempt_token": None, "lease_expires_at": None,
        "reason": "QUEUED", **changes,
    }))


def insert_notice(factory):
    with factory.begin() as db:
        db.add(notice())


def snapshot(factory, excluded=()):
    with factory() as db:
        return {table.name: db.execute(select(table).order_by(*table.primary_key.columns)).all()
                for table in Base.metadata.sorted_tables if table.name not in excluded}


@pytest.mark.parametrize("changes,state", [
    ({"notification_id": " "}, "23514"), ({"request_id": " "}, "23514"), ({"business_id": " "}, "23514"),
    ({"source_module": "D"}, "23514"), ({"source_module": "M2"}, "23514"), ({"kind": "M3_UNKNOWN"}, "23514"),
    ({"channel": "SMS"}, "23514"), ({"title": " "}, "23514"), ({"title": "x" * 2001}, "23514"),
    ({"title": "x\ny"}, "23514"), ({"body": " "}, "23514"), ({"body": "x" * 2001}, "23514"),
    ({"retry_limit": -1}, "23514"), ({"retry_limit": 6}, "23514"), ({"input_data": []}, "23514"),
    ({"input_data": None}, "23514"), ({"space_id": "missing"}, "23503"),
])
def test_database_enforces_notification_constraints(notification_db, changes, state):
    with pytest.raises(IntegrityError) as caught:
        with notification_db.begin() as db:
            db.add(notice(**changes))
    assert caught.value.orig.sqlstate == state


def test_request_namespace_unique_per_source_allows_other_sources(notification_db):
    insert_notice(notification_db)
    with pytest.raises(IntegrityError) as caught:
        with notification_db.begin() as db:
            db.add(notice(notification_id="n-duplicate"))
    assert caught.value.orig.sqlstate == "23505"
    with notification_db.begin() as db:
        db.add(notice(notification_id="n-m2", source_module="M2", kind="M2_APPROVED"))
    with notification_db() as db:
        assert db.scalar(select(func.count()).select_from(Notification)) == 2


@pytest.mark.parametrize("changes,state", [
    ({"recipient_order": -1}, "23514"), ({"status": "DELIVERED"}, "23514"),
    ({"attempt_count": -1}, "23514"), ({"attempt_count": 7, "last_attempt_at": NOW}, "23514"),
    ({"last_attempt_at": NOW}, "23514"), ({"attempt_count": 1}, "23514"),
    ({"next_attempt_at": None}, "23514"), ({"status": "FAILED"}, "23514"),
    ({"attempt_token": "unclaimed"}, "23514"), ({"lease_expires_at": NOW}, "23514"),
    ({"status": "SUBMITTED", "next_attempt_at": None}, "23514"),
    ({"status": "UNKNOWN", "next_attempt_at": None}, "23514"),
    ({"recipient_email": " "}, "23514"), ({"recipient_email": "x\ny@example.test"}, "23514"),
    ({"reason": " "}, "23514"), ({"notification_id": "missing"}, "23503"),
    ({"recipient_user_id": "missing"}, "23503"),
])
def test_database_enforces_delivery_constraints(notification_db, changes, state):
    insert_notice(notification_db)
    with pytest.raises(IntegrityError) as caught:
        with notification_db.begin() as db:
            db.add(delivery(**changes))
    assert caught.value.orig.sqlstate == state


@pytest.mark.parametrize("state", ["PENDING", "SENDING", "SUBMITTED", "FAILED", "UNKNOWN", "RETRY"])
def test_legal_delivery_states_persist_with_attempt_metadata(notification_db, state):
    insert_notice(notification_db)
    fields = {}
    if state in {"SENDING", "SUBMITTED", "UNKNOWN", "RETRY"}:
        fields.update(attempt_count=1, last_attempt_at=NOW)
    if state not in {"PENDING", "RETRY"}:
        fields.update(status=state, next_attempt_at=None)
    if state == "SENDING":
        fields.update(attempt_token="test-claim-token", lease_expires_at=NOW + timedelta(seconds=60))
    elif state == "FAILED":
        fields.update(recipient_email=None, reason="EMAIL_MISSING")
    elif state == "RETRY":
        fields.update(next_attempt_at=NOW + timedelta(seconds=30), reason="RETRY_SCHEDULED")
    with notification_db.begin() as db:
        db.add(delivery(**fields))
    with notification_db() as db:
        row = db.get(NotificationDelivery, ("n-test", "u-student1"))
        assert row.status == ("PENDING" if state == "RETRY" else state)
        assert row.attempt_count == (1 if state in {"SENDING", "SUBMITTED", "UNKNOWN", "RETRY"} else 0)


@pytest.mark.parametrize("changes", [
    {"attempt_token": None}, {"attempt_token": " "}, {"lease_expires_at": None},
    {"lease_expires_at": NOW}, {"recipient_email": None},
])
def test_sending_requires_address_and_valid_claim(notification_db, changes):
    insert_notice(notification_db)
    fields = {"status": "SENDING", "attempt_count": 1, "last_attempt_at": NOW, "next_attempt_at": None,
              "attempt_token": "test-claim", "lease_expires_at": NOW + timedelta(seconds=60), **changes}
    with pytest.raises(IntegrityError) as caught:
        with notification_db.begin() as db:
            db.add(delivery(**fields))
    assert caught.value.orig.sqlstate == "23514"


def test_recipient_and_position_are_unique_but_other_people_are_independent(notification_db):
    insert_notice(notification_db)
    with notification_db.begin() as db:
        db.add(delivery())
    for fields in ({"recipient_order": 1}, {"recipient_user_id": "u-technician"}):
        with pytest.raises(IntegrityError) as caught:
            with notification_db.begin() as db:
                db.add(delivery(**fields))
        assert caught.value.orig.sqlstate == "23505"
    with notification_db.begin() as db:
        db.add(delivery(recipient_user_id="u-technician", recipient_order=1, recipient_email=None,
                        status="FAILED", next_attempt_at=None, reason="EMAIL_MISSING"))
    with notification_db() as db:
        rows = db.scalars(select(NotificationDelivery).order_by(NotificationDelivery.recipient_order)).all()
        assert [(r.recipient_user_id, r.status, r.attempt_count) for r in rows] == [
            ("u-student1", "PENDING", 0), ("u-technician", "FAILED", 0),
        ]


def test_notification_and_recipients_rollback_together_on_invalid_reference(notification_db):
    with pytest.raises(IntegrityError):
        with notification_db.begin() as db:
            db.add(notice())
            db.flush()
            db.add(delivery())
            db.add(delivery(recipient_user_id="missing", recipient_order=1))
    with notification_db() as db:
        assert db.scalar(select(func.count()).select_from(Notification)) == 0
        assert db.scalar(select(func.count()).select_from(NotificationDelivery)) == 0


def test_repeated_seed_preserves_notice_attempts_and_retry_snapshot(notification_db):
    with notification_db.begin() as db:
        db.add(notice())
        db.flush()
        db.add(delivery(status="UNKNOWN", attempt_count=1, last_attempt_at=NOW, next_attempt_at=None,
                        reason="SMTP_RESULT_UNKNOWN"))
        config = db.get(PublicConfig, 1)
        config.notification_retry_limit = 5
        config.version = 2
    before = snapshot(notification_db)
    seed_module.seed()
    unchanged = snapshot(notification_db) == before
    assert unchanged, "Repeated seed must preserve all current data"
    with notification_db() as db:
        assert db.get(Notification, "n-test").retry_limit == 2
        assert db.get(PublicConfig, 1).notification_retry_limit == 5


def test_reference_history_cannot_be_deleted_while_delivery_exists(notification_db):
    insert_notice(notification_db)
    with notification_db.begin() as db:
        db.add(delivery())
    for model, condition in [
        (Notification, Notification.notification_id == "n-test"),
        (User, User.user_id == "u-student1"), (Space, Space.space_id == "sp-school"),
    ]:
        with pytest.raises(IntegrityError) as caught:
            with notification_db.begin() as db:
                db.execute(delete(model).where(condition))
        assert caught.value.orig.sqlstate == "23503"


def test_migration_preserves_prior_thirteen_tables_and_creates_query_indexes(notification_db):
    before = snapshot(notification_db, NEW_TABLES)
    migration = ScriptDirectory(str(ROOT / "backend/d/migrations")).get_revision("d_c8215f90e4ab").module
    with notification_db.begin() as db:
        assert re.fullmatch(r"d_seed_test_[0-9a-f]{32}", db.scalar(text("SELECT current_schema()")))
        with Operations.context(MigrationContext.configure(db.connection())):
            migration.downgrade()
            migration.upgrade()
        for model in (Notification, NotificationDelivery):
            assert db.scalar(select(func.count()).select_from(model)) == 0
        index_names = {i["name"] for i in inspect(db.connection()).get_indexes("notification_deliveries")}
        assert {"ix_notification_deliveries_due", "ix_notification_deliveries_lease"} <= index_names
    unchanged = snapshot(notification_db, NEW_TABLES) == before
    assert unchanged, "Migration must preserve all prior data"
    seed_module.seed()
    unchanged = snapshot(notification_db, NEW_TABLES) == before
    assert unchanged
    with notification_db() as db:
        for model in (Notification, NotificationDelivery):
            assert db.scalar(select(func.count()).select_from(model)) == 0
