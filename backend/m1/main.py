from backend.m1.api import router
from backend.m1.tasks import tick
from backend.shared.factory import create_app

app = create_app("m1", router, tick)
