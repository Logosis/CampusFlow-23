"""真实重放迁移；只有可证明版本一致的历史配置才回填。"""
from datetime import datetime, timezone
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import sqlalchemy as sa


def test_snapshot_migration_backfills_only_matching_versions(tmp_path):
    engine = sa.create_engine("sqlite:///" + (tmp_path / "migration.sqlite").as_posix())
    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[2] / "backend/m2/migrations"))
    revisions = list(reversed(list(scripts.walk_revisions())))
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            for revision in revisions[:-1]:
                revision.module.upgrade()
            metadata = sa.MetaData()
            config = sa.Table("m2_approval_config", metadata, autoload_with=connection)
            apps = sa.Table("m2_application", metadata, autoload_with=connection)
            now = datetime.now(timezone.utc)
            connection.execute(config.insert().values(id=1, version=2, application_scopes=[],
                approval_levels=[{"level": 1, "approverId": "original"}], clock_start="CURRENT_LEVEL",
                timeout_seconds=100, check_interval_seconds=1, updated_at=now))
            for version in (1, 2):
                connection.execute(apps.insert().values(application_id=f"app-{version}", applicant_id="u-student1",
                    space_id="sp-school", start_time=now, end_time=now, purpose="migration",
                    status="PENDING", current_level=1, version=1, config_version=version,
                    submitted_at=now, current_level_started_at=now))
            revisions[-1].module.upgrade()
            refreshed = sa.Table("m2_application", sa.MetaData(), autoload_with=connection)
            rows = dict(connection.execute(sa.select(refreshed.c.config_version, refreshed.c.approval_config_snapshot)).all())
            assert rows[1] is None
            assert rows[2]["approvalLevels"] == [{"level": 1, "approverId": "original"}]
            revisions[-1].module.downgrade()
            assert "approval_config_snapshot" not in {c["name"] for c in sa.inspect(connection).get_columns("m2_application")}
            revisions[-1].module.upgrade()
    engine.dispose()
