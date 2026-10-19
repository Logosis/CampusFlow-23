from backend.m2.api import router
from backend.m2.service import tick
from backend.shared.factory import create_app

app = create_app("m2", router, tick)
