from backend.d.api import router
from backend.shared.factory import create_app

app = create_app("d", router)
# D实现后在此设置 app.state.identity_resolver = 自己的异步凭据校验函数。
# 本模块定时任务实现后，把异步 tick 函数传给 create_app。
