"""Idempotently install the common M3 timeout settings after checking D master IDs."""
import asyncio
import json
from pathlib import Path
from backend.m3 import domain as db, peers
from backend.m3.models import TimeoutConfig
from backend.m3.schemas import ConfigOutput
from backend.shared.errors import ApiError
from backend.shared.settings import Settings


async def seed_async():
    if Settings.load('m3').app_env != 'test':
        raise ApiError('FORBIDDEN', '共同测试初始化仅允许test环境')
    fixture = json.loads((Path(__file__).resolve().parents[2] / 'tests/fixtures/bootstrap.json').read_text())
    cfg = ConfigOutput.model_validate(fixture['m3']).model_dump()
    for expected in fixture['spaces']:
        actual = await peers.get_space(expected['spaceId'])
        if any(actual[k] != expected[k] for k in ('spaceId', 'departmentId', 'registered', 'isStudyRoom')):
            raise ApiError('CONFLICT', 'D测试空间与共同初始化不一致')
    for account in ('dispatcher', 'technician', 'inspector', 'supervisor'):
        ident = next(u['userId'] for u in fixture['users'] if u['account'] == account)
        purpose, code = {'dispatcher': ('DISPLAY', None),
            'technician': ('DISPATCH', 'm3.repair.manage'), 'inspector': ('ACCEPTANCE', 'm3.acceptance.manage'),
            'supervisor': ('ESCALATION', 'm3.escalation.receive')}[account]
        rows = await peers.people('sp-school', purpose, [code] if code else [], user_ids=[ident])
        if rows[0]['accountStatus'] != 'ACTIVE' or code and not rows[0]['eligible']:
            raise ApiError('CONFLICT', 'D测试人员资格不符合共同初始化')
    with db.sessions()() as session, session.begin():
        db.lock_key(session, 'm3:config')
        if session.get(TimeoutConfig, 1) is None:
            session.add(TimeoutConfig(id=1, data=cfg))


def seed():
    asyncio.run(seed_async())
