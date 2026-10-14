import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, APIRouter, Body, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from backend.shared.access import guard
from backend.shared.errors import ApiError, failure, success
from backend.shared.registry import SERVICES
from backend.shared.settings import Settings

def create_app(module: str, router: APIRouter, tick=None) -> FastAPI:
    known = SERVICES[module]
    implemented = set()
    for route in router.routes:
        name = route.path.removeprefix("/")
        if route.path != f"/{name}" or name not in known or route.methods != {"POST"}:
            raise ValueError(f"{module}: 未登记的公开路径或方法 {route.path}")
        if name in implemented:
            raise ValueError(f"{module}: 重复路径 {name}")
        implemented.add(name)

    async def worker(app):
        while True:
            try:
                await tick()
                app.state.worker_error = False
            except Exception:
                # 不删除业务状态；日志与补偿由模块实现，健康状态明确报错。
                app.state.worker_error = True
            await asyncio.sleep(1)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(worker(app)) if tick else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(title=f"CampusFlow {module.upper()}", lifespan=lifespan)
    app.state.settings = Settings.load(module)
    app.state.worker_error = False
    app.state.implemented = implemented

    @app.exception_handler(ApiError)
    async def api_error(request, exc):
        return failure(exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return failure("INVALID_ARGUMENT", "请求字段、类型或格式不符合接口约定")

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return failure("NOT_FOUND" if exc.status_code == 404 else "INVALID_ARGUMENT",
                       "接口不存在" if exc.status_code == 404 else "请求方法或内容不合法")

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        return failure("INTERNAL_ERROR", "内部处理失败，请保留原请求编号核实")

    @app.middleware("http")
    async def access_guard(request: Request, call_next):
        prefix = f"/api/v1/{module}/"
        function = request.url.path.removeprefix(prefix)
        if request.method == "POST" and request.url.path.startswith(prefix) and function in implemented:
            try:
                await guard(request, function)
            except ApiError as exc:
                return failure(exc.code, exc.message)
            except Exception:
                return failure("INTERNAL_ERROR", "身份检查失败")
        return await call_next(request)

    @app.get("/health")
    async def health():
        missing = [n for n, item in known.items() if item["required"] and n not in implemented]
        return success({"module": module.upper(), "unfinishedFunctions": missing,
                        "workerConfigured": tick is not None, "workerError": app.state.worker_error})

    app.include_router(router, prefix=f"/api/v1/{module}")
    def unfinished(name):
        async def endpoint(input: dict = Body(...)):
            raise ApiError("UNAVAILABLE", f"{module.upper()}.{name}尚未实现")
        return endpoint
    for name in known:
        if name not in implemented:
            app.add_api_route(f"/api/v1/{module}/{name}", unfinished(name), methods=["POST"],
                             tags=["unimplemented"], name=name)
    return app
