"""Optional LOCAL browser test server. D/M1 are doubles; M3 and PostgreSQL are real.
Run: .venv/bin/python -m tests.m3.browser_server
Never deploy this test harness or expose its loopback port externally.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4
import httpx
import uvicorn
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from backend.m3 import domain, peers, service, worker
from backend.m3.api import router
from backend.m3.models import Base
from backend.m3.seed import seed_async
from backend.shared import client as shared_client, clock
from backend.shared.errors import failure, success
from backend.shared.factory import create_app
from backend.shared.settings import Settings
from tests.m3.conftest import PeerDouble

ROOT = Path(__file__).resolve().parents[2]
AUTH = '''export function getCredential(){return sessionStorage.getItem('campusflow.credential');}
export function clearSession(){sessionStorage.removeItem('campusflow.credential');}
export function setSession(data){sessionStorage.setItem('campusflow.credential',data.credential);}
export async function api(module,fn,input){
 const token=getCredential();let response,body;
 try{response=await fetch('/api/v1/'+module+'/'+fn,{method:'POST',headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})},body:JSON.stringify(input)});body=await response.json();}
 catch{throw Object.assign(new Error('network'),{code:'UNAVAILABLE',httpStatus:null});}
 if(!body.ok){if(response.status===401)clearSession();throw Object.assign(new Error(body.error.message),{code:body.error.code,httpStatus:response.status});}return body.data;}
'''


def make_app():
    settings = Settings.load('m3')
    if settings.app_env != 'test':
        raise RuntimeError('Browser doubles require APP_ENV=test')
    schema = 'm3_browser_' + uuid4().hex
    engine = create_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA {schema}'))
    isolated = create_engine(settings.database_url, connect_args={'options': f'-csearch_path={schema}'})
    Base.metadata.create_all(isolated)
    factory = sessionmaker(bind=isolated, expire_on_commit=False)
    domain.sessions = lambda: factory
    fake = PeerDouble()
    for account in ('student1', 'student2'):
        for code in ('m3.work_order.read', 'm3.work_order.cancel'):
            fake.grant(account, code)
    for code in ('m3.work_order.read_all', 'm3.statistics.read'):
        fake.grant('admin', code)
    original = httpx.AsyncClient
    def client(*args, **kwargs):
        kwargs['transport'] = httpx.MockTransport(fake.handler)
        return original(*args, **kwargs)
    shared_client.httpx.AsyncClient = client
    fixed = lambda: datetime(2026, 10, 15, 4, tzinfo=timezone.utc)
    domain.utc_now = service.utc_now = clock.utc_now = fixed
    asyncio.run(seed_async())
    app = create_app('m3', router, tick=worker.tick)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        try:
            async with original_lifespan(app):
                yield
        finally:
            isolated.dispose()
            with engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA {schema} CASCADE'))
            engine.dispose()
    app.router.lifespan_context = lifespan
    app.state.lose_report = False

    @app.middleware('http')
    async def lost_response(request, call_next):
        response = await call_next(request)
        if request.url.path == '/api/v1/m3/report_fault' and app.state.lose_report:
            app.state.lose_report = False
            return failure('UNAVAILABLE', 'Browser test: committed response lost')
        return response

    @app.post('/__test__/lose-next-report')
    async def lose_report():
        app.state.lose_report = True
        return {'testOnly': True}

    @app.get('/__test__/signin/{account}')
    async def signin(account: str):
        if account not in fake.users:
            return HTMLResponse('Unknown test account', status_code=404)
        value = json.dumps(account)
        return HTMLResponse('<h1>仅本地M3隔离测试；D/M1为模拟</h1><script>sessionStorage.setItem("campusflow.credential",'
            + value + ');location.replace("/m3/");</script>')

    @app.get('/shared/auth.js')
    async def auth_js():
        from fastapi.responses import Response
        return Response(AUTH, media_type='text/javascript')

    @app.post('/api/v1/d/get_identity')
    async def identity(request: Request):
        return success(await peers.peer('d', 'get_identity', {}, request))

    app.mount('/m3', StaticFiles(directory=ROOT / 'frontend/m3', html=True), name='m3')
    return app


if __name__ == '__main__':
    uvicorn.run(make_app(), host='127.0.0.1', port=8913, log_level='warning')
