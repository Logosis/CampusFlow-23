from backend.d.api import router
from backend.d.sessions import resolve_identity
from backend.shared.factory import create_app

app = create_app("d", router)
app.state.identity_resolver = resolve_identity
# 本模块定时任务实现后，把异步 tick 函数传给 create_app。
