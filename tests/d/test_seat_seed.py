"""真实迁移和隔离数据库中的座位初始化、约束及原有数据保留。"""

import json
import re

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

import backend.d.seed as seed_module
from backend.d.models import Base, Seat, Space
from backend.shared.settings import ROOT


def snapshot(factory, exclude=()):
    with factory() as db:
        return {
            table.name: db.execute(select(table).order_by(*table.primary_key.columns)).all()
            for table in Base.metadata.sorted_tables if table.name not in exclude
        }


def use_fixture(fixture, tmp_path, monkeypatch):
    target = tmp_path / "tests/fixtures/bootstrap.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(fixture), encoding="utf-8")
    monkeypatch.setattr(seed_module, "ROOT", tmp_path)


def test_seed_creates_exact_shared_seats_with_real_space_references(isolated_db, fixture_data):
    seed_module.seed()
    with isolated_db() as db:
        assert db.scalar(select(func.count()).select_from(Seat)) == 3
        for item in fixture_data["seats"]:
            seat = db.get(Seat, item["seatId"])
            assert db.get(Space, seat.space_id) is not None
            assert {
                "seatId": seat.seat_id, "seatCode": seat.seat_code, "spaceId": seat.space_id,
                "registered": seat.registered, "enabled": seat.enabled,
                "socketTags": seat.socket_tags, "version": seat.version,
            } == item


def test_repeat_preserves_disabled_unregistered_and_edited_seats(isolated_db):
    seed_module.seed()
    with isolated_db.begin() as db:
        seat = db.get(Seat, "seat-school-1")
        seat.registered = False
        seat.enabled = False
        seat.socket_tags = ["RAIL", "FIXED"]
        seat.version = 2
        db.get(Seat, "seat-school-2").enabled = False
    before = snapshot(isolated_db)
    seed_module.seed()
    unchanged = snapshot(isolated_db) == before
    assert unchanged, "Repeated seed must preserve seat state and all existing records"


@pytest.mark.parametrize("changes", [
    {"seatId": " "}, {"seatCode": ""}, {"seatCode": 123},
    {"spaceId": "sp-missing"}, {"registered": "true"}, {"enabled": 1},
    {"version": 0}, {"version": True},
    {"socketTags": None}, {"socketTags": "FIXED"}, {"socketTags": ["UNKNOWN"]},
    {"socketTags": ["FIXED", "FIXED"]}, {"socketTags": [{}]},
])
def test_invalid_seat_fixture_rolls_back_all_new_records(
    isolated_db, fixture_data, tmp_path, monkeypatch, changes,
):
    # 第三个座位损坏时，前两个及本次创建的其他数据也应回滚。
    fixture_data["seats"][2].update(changes)
    use_fixture(fixture_data, tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="Invalid seat"):
        seed_module.seed()
    with isolated_db() as db:
        for table in Base.metadata.sorted_tables:
            assert db.scalar(select(func.count()).select_from(table)) == 0


def test_seed_does_not_add_registered_seat_to_unregistered_space(isolated_db):
    seed_module.seed()
    with isolated_db.begin() as db:
        db.execute(delete(Seat).where(Seat.space_id == "sp-cs"))
        db.get(Space, "sp-cs").registered = False
    before = snapshot(isolated_db)
    with pytest.raises(ValueError, match="Invalid seat space"):
        seed_module.seed()
    unchanged = snapshot(isolated_db) == before
    assert unchanged, "Rejected seat initialization must not overwrite existing records"


@pytest.mark.parametrize("changes,sqlstate", [
    ({"seat_id": " "}, "23514"),
    ({"seat_code": " "}, "23514"),
    ({"seat_code": "TEST-S-001"}, "23505"),
    ({"space_id": "sp-missing"}, "23503"),
    ({"version": 0}, "23514"),
    ({"socket_tags": ["UNKNOWN"]}, "23514"),
    ({"socket_tags": ["FIXED", "FIXED"]}, "23514"),
    ({"socket_tags": ["RAIL", "FIXED", "RAIL"]}, "23514"),
    ({"socket_tags": None}, "23514"),
    ({"socket_tags": {}}, "23514"),
    ({"socket_tags": "FIXED"}, "23514"),
])
def test_database_enforces_seat_constraints(isolated_db, changes, sqlstate):
    seed_module.seed()
    with pytest.raises(IntegrityError) as caught:
        with isolated_db.begin() as db:
            db.add(Seat(**({
                "seat_id": "seat-invalid", "seat_code": "TEST-INVALID",
                "space_id": "sp-school", "registered": True, "enabled": True,
                "socket_tags": [], "version": 1,
            } | changes)))
    assert caught.value.orig.sqlstate == sqlstate


@pytest.mark.parametrize("tags", [[], ["FIXED"], ["RAIL"], ["FIXED", "RAIL"], ["RAIL", "FIXED"]])
def test_all_allowed_socket_combinations_are_stored(isolated_db, tags):
    seed_module.seed()
    with isolated_db.begin() as db:
        db.get(Seat, "seat-cs-1").socket_tags = tags
    with isolated_db() as db:
        assert db.get(Seat, "seat-cs-1").socket_tags == tags


def test_unregistered_seat_still_reserves_its_code(isolated_db):
    seed_module.seed()
    with isolated_db.begin() as db:
        db.get(Seat, "seat-school-1").registered = False
    with pytest.raises(IntegrityError) as caught:
        with isolated_db.begin() as db:
            db.add(Seat(
                seat_id="seat-reused", seat_code="TEST-S-001", space_id="sp-cs",
                registered=True, enabled=True, socket_tags=[], version=1,
            ))
    assert caught.value.orig.sqlstate == "23505"


def test_space_with_seats_cannot_be_deleted_by_database(isolated_db):
    seed_module.seed()
    with pytest.raises(IntegrityError) as caught:
        with isolated_db.begin() as db:
            db.execute(delete(Space).where(Space.space_id == "sp-school"))
    assert caught.value.orig.sqlstate == "23503"
    with isolated_db() as db:
        assert db.get(Space, "sp-school") is not None
        assert db.scalar(select(func.count()).select_from(Seat)) == 3


def test_seat_migration_preserves_existing_identity_and_permission_data(isolated_db):
    seed_module.seed()
    before = snapshot(isolated_db, exclude={"seats"})
    scripts = ScriptDirectory(str(ROOT / "backend/d/migrations"))
    migration = scripts.get_revision("d_ac2072660bc7").module
    with isolated_db.begin() as db:
        schema = db.scalar(text("SELECT current_schema()"))
        assert re.fullmatch(r"d_seed_test_[0-9a-f]{32}", schema)
        with Operations.context(MigrationContext.configure(db.connection())):
            migration.downgrade()
            migration.upgrade()
        assert db.scalar(select(func.count()).select_from(Seat)) == 0
    unchanged = snapshot(isolated_db, exclude={"seats"}) == before
    assert unchanged
    seed_module.seed()
    unchanged = snapshot(isolated_db, exclude={"seats"}) == before
    assert unchanged
    with isolated_db() as db:
        assert db.scalar(select(func.count()).select_from(Seat)) == 3
