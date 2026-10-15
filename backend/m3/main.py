from backend.m3.api import router
from backend.m3.worker import tick
from backend.shared.factory import create_app

app = create_app("m3", router, tick=tick)
