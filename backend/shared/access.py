from backend.shared.auth import bearer, current_user, service_identity
from backend.shared.errors import ApiError

# 这里只检查调用来源、模式、登录；具体权限和业务关系由提供方继续检查。
PEERS = {
    "d": {
        "get_identity": {"m1", "m2", "m3"},
        "check_permission": {"m1", "m2", "m3"},
        "manage_roles": {"m1"}, "query_master": {"m1", "m2", "m3"},
        "maintain_master": {"m1"}, "query_people": {"m2", "m3"},
        "send_notification": {"m1", "m2", "m3"},
        "get_notification": {"m1", "m2", "m3"},
        "audit": {"m1", "m2", "m3"}, "manage_public_config": {"m1"},
    },
    "m1": {"get_room_availability": {"m2"}, "change_room_limit": {"m2", "m3"},
           "get_room_limit": {"m2", "m3"}},
    "m2": {"get_approval_config": {"m1"}, "update_approval_config": {"m1"}},
    "m3": {"get_timeout_config": {"m1"}, "update_timeout_config": {"m1"}},
}
DIRECT = {
    "d": {"login", "logout", "get_identity", "get_notification", "audit"},
    "m1": {"get_config", "update_config", "manage_roles", "query_master", "maintain_master",
           "get_opening_times", "set_opening_times", "search_seats", "reserve_seat",
           "get_check_in_code", "check_in", "get_my_reservations", "get_my_reservation", "get_usage"},
    "m2": {"get_allowed_actions", "submit_application", "get_approval_tasks", "submit_approval",
           "get_application"},
    "m3": {"report_fault", "get_dispatch_tasks", "dispatch_order", "get_work_tasks", "start_repair",
           "submit_completion", "submit_acceptance", "submit_evaluation",
           "get_dispatch_candidates", "get_report_locations"},
}
USER_ONLY = {"get_identity", "manage_roles", "maintain_master", "manage_public_config",
             "get_approval_config", "update_approval_config", "get_timeout_config", "update_timeout_config"}

async def guard(request, function):
    settings = request.app.state.settings
    module = settings.module
    peer = service_identity(request.headers.get("X-Service-Credential"), settings)
    mode = request.headers.get("X-Actor-Mode")
    token = bearer(request)
    if peer is None:
        if mode is not None:
            raise ApiError("INVALID_ARGUMENT", "前端不能指定后台调用模式")
        if function not in DIRECT[module]:
            raise ApiError("FORBIDDEN", "此服务不向前端直接开放")
        request.state.actor_mode = "USER"
        request.state.source_module = None
        if module == "d" and function == "audit":
            try:
                payload = await request.json()
            except ValueError as exc:
                raise ApiError("INVALID_ARGUMENT", "请求必须是JSON") from exc
            if isinstance(payload, dict) and payload.get("operation") == "APPEND":
                raise ApiError("FORBIDDEN", "前端不能追加来源模块的审计事件")
        if module == "d" and function == "login":
            return
        if module == "d" and function == "logout":
            if token is None:
                raise ApiError("UNAUTHENTICATED", "需要原用户凭据")
            # 已撤销或到期的原凭据仍由D退出实现安全确认，不再要求当前登录有效。
            return
    else:
        if peer not in PEERS[module].get(function, set()):
            raise ApiError("FORBIDDEN", "调用模块没有此服务资格")
        if mode not in {"USER", "SYSTEM"}:
            raise ApiError("INVALID_ARGUMENT", "模块调用必须指定USER或SYSTEM")
        if mode == "SYSTEM" and token is not None:
            raise ApiError("INVALID_ARGUMENT", "后台模式不能携带用户凭据")
        if function in USER_ONLY and mode != "USER":
            raise ApiError("FORBIDDEN", "此服务必须传递用户身份")
        request.state.source_module = peer
        request.state.actor_mode = mode
        if mode == "SYSTEM":
            return
    request.state.identity = await current_user(request)
