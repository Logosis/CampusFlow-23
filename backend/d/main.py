from backend.d.api import router
from backend.d.notification_worker import tick
from backend.d.sessions import resolve_identity
from backend.shared.factory import create_app

app = create_app("d", router, tick=tick)
app.state.identity_resolver = resolve_identity
