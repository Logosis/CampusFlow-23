"""D and M1 access exclusively through the shared HTTP client."""
from pydantic import ValidationError
from backend.shared.auth import bearer
from backend.shared.client import Actor, call_peer
from backend.shared.errors import ApiError
from backend.shared.settings import Settings
from backend.m3.schemas import Page, Person, Space, PermissionResult


async def peer(target, function, body, request=None):
    actor = Actor('USER', bearer(request)) if request else Actor('SYSTEM')
    settings = request.app.state.settings if request else Settings.load('m3')
    return await call_peer(settings, target, function, body, actor)


def checked(model, value):
    try:
        return model.model_validate(value).model_dump()
    except (ValidationError, TypeError, ValueError):
        raise ApiError('UNAVAILABLE', '依赖服务返回不符合接口约定') from None


async def permission(code, request, space_id=None, *, required=True, global_scope=False):
    body = {'permissionCode': code}
    if space_id:
        body['targetScope'] = {'type': 'SPACE', 'id': space_id}
    elif global_scope:
        body['targetScope'] = {'type': 'GLOBAL', 'id': None}
    result = checked(PermissionResult, await peer('d', 'check_permission', body, request))
    if result['subjectType'] != 'USER' or result['subjectId'] != request.state.identity['userId']:
        raise ApiError('UNAVAILABLE', '权限判断主体与当前身份不一致')
    if not result['allowed'] and required:
        raise ApiError('FORBIDDEN', '没有此操作的权限或范围')
    return result['allowedScopes'] if result['allowed'] else []


async def get_space(space_id, request=None):
    return checked(Space, await peer('d', 'query_master',
        {'operation': 'GET_SPACE', 'spaceId': space_id, 'includeUnregistered': True}, request))


async def collect(target, function, body, model, request=None):
    rows, seen = [], set()
    while True:
        result = checked(Page[model], await peer(target, function, body | {'limit': 100}, request))
        rows.extend(result['items'])
        cursor = result['nextCursor']
        if cursor is None:
            return rows
        if cursor in seen:
            raise ApiError('UNAVAILABLE', '依赖分页游标重复')
        seen.add(cursor)
        body = body | {'cursor': cursor}


async def people(space_id, purpose, codes, request=None, user_ids=None):
    body = dict(mode='BY_IDS' if user_ids is not None else 'CANDIDATES', purpose=purpose,
        permissionCodes=codes, targetScope={'type': 'SPACE', 'id': space_id})
    if user_ids is None:
        rows = await collect('d', 'query_people', body, Person, request)
        if any(p['accountStatus'] != 'ACTIVE' or p['eligible'] is not True
               or not set(codes).issubset(p['matchedPermissionCodes']) for p in rows):
            raise ApiError('UNAVAILABLE', '候选人员返回包含不合格记录')
        return rows
    result = await peer('d', 'query_people', body | {'userIds': user_ids}, request)
    if not isinstance(result, dict) or set(result) != {'people'} or not isinstance(result['people'], list):
        raise ApiError('UNAVAILABLE', '人员返回不符合约定')
    rows = [checked(Person, p) for p in result['people']]
    if purpose != 'DISPLAY' and any(p['eligible'] is None or p['eligible'] is True
            and not set(codes).issubset(p['matchedPermissionCodes']) for p in rows):
        raise ApiError('UNAVAILABLE', '人员资格回执与请求不一致')
    if [p['userId'] for p in rows] != user_ids:
        raise ApiError('UNAVAILABLE', '人员结果与请求不一致')
    return rows
