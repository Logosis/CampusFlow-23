"""消费者构造的后台事件须通过真实D模型；这里不替代数据库或HTTP集成验收。"""
from types import SimpleNamespace

from backend.d.audit_schemas import AuditAppendInput
from backend.m3.domain import record_event
from backend.m3.models import Outbox


def test_m3_timeout_event_matches_d_system_actor_contract():
    rows = []
    recording_session = SimpleNamespace(add=rows.append)
    work_order = SimpleNamespace(id="work-contract", space_id="sp-school", version=2)
    record_event(recording_session, work_order, "ESCALATE", None, "超时升级")
    job = next(row for row in rows if isinstance(row, Outbox))
    event = AuditAppendInput.model_validate(job.payload)
    assert event.actorType == "SYSTEM" and event.actorId == "M3"
