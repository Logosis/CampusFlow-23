from backend.m1.api import router
from backend.shared.factory import create_app

app = create_app("m1", router)
# 本模块定时任务实现后，把异步 tick 函数传给 create_app。
