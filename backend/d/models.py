from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Index,
    JSON,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Department(Base):
    """院系资料。"""

    __tablename__ = "departments"

    department_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class User(Base):
    """用户账号与基本资料。"""

    __tablename__ = "users"

    __table_args__ = (
        CheckConstraint(
            "account_status IN ('ACTIVE', 'DISABLED')",
            name="ck_users_account_status",
        ),
        CheckConstraint(
            "role_assignment_version >= 1",
            name="ck_users_role_assignment_version",
        ),
    )

    user_id: Mapped[str] = mapped_column(Text, primary_key=True)

    account: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True
    )

    password_hash: Mapped[str] = mapped_column(
        Text, nullable=False
    )

    display_name: Mapped[str] = mapped_column(
        Text, nullable=False
    )

    department_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("departments.department_id"),
        nullable=True,
        index=True,
    )

    email: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )

    account_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="ACTIVE"
    )

    role_assignment_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )


class Role(Base):
    """角色定义，例如学生、管理员、维修人员。"""

    __tablename__ = "roles"

    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED')",
            name="ck_roles_status",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_roles_version",
        ),
    )

    role_id: Mapped[str] = mapped_column(
        Text, primary_key=True
    )

    name: Mapped[str] = mapped_column(
        Text, nullable=False
    )

    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="ACTIVE"
    )

    version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )


class Permission(Base):
    """权限目录，记录系统支持的操作及允许的范围类型。"""

    __tablename__ = "permissions"

    code: Mapped[str] = mapped_column(
        Text, primary_key=True
    )

    name: Mapped[str] = mapped_column(
        Text, nullable=False
    )

    allowed_scope_types: Mapped[list[str]] = mapped_column(
        JSON, nullable=False
    )


class UserRole(Base):
    """记录用户拥有的角色。"""

    __tablename__ = "user_roles"

    user_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("users.user_id"),
        primary_key=True,
    )

    role_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("roles.role_id"),
        primary_key=True,
    )


class RoleGrant(Base):
    """记录角色获得的权限及其授权范围。"""

    __tablename__ = "role_grants"

    __table_args__ = (
        CheckConstraint(
            "(scope_type = 'GLOBAL' AND scope_id IS NULL) OR "
            "(scope_type IN ('DEPARTMENT', 'SPACE') "
            "AND scope_id IS NOT NULL AND length(trim(scope_id)) > 0)",
            name="ck_role_grants_scope",
        ),
        UniqueConstraint(
            "role_id",
            "permission_code",
            "scope_type",
            "scope_id",
            name="uq_role_grants_scope",
            postgresql_nulls_not_distinct=True,
        ),
    )

    grant_id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    role_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("roles.role_id"),
        nullable=False,
    )

    permission_code: Mapped[str] = mapped_column(
        Text,
        ForeignKey("permissions.code"),
        nullable=False,
    )

    scope_type: Mapped[str] = mapped_column(
        Text, nullable=False
    )

    scope_id: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )


class LoginSession(Base):
    """记录一次成功登录及其有效期、退出状态。"""

    __tablename__ = "login_sessions"

    __table_args__ = (
        UniqueConstraint(
            "login_account",
            "request_id",
            name="uq_login_sessions_account_request",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_login_sessions_expiry",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_login_sessions_revoked_at",
        ),
    )

    session_id: Mapped[str] = mapped_column(
        Text, primary_key=True
    )

    user_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("users.user_id"),
        nullable=False,
        index=True,
    )

    login_account: Mapped[str] = mapped_column(
        Text, nullable=False
    )

    request_id: Mapped[str] = mapped_column(
        Text, nullable=False
    )

    login_password_hash: Mapped[str] = mapped_column(
        Text, nullable=False
    )

    credential_hash: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Space(Base):
    """统一空间资料；院系归属供范围权限判断使用。"""

    __tablename__ = "spaces"
    __table_args__ = (
        CheckConstraint(
            "(owner_kind = 'SCHOOL' AND department_id IS NULL) OR "
            "(owner_kind = 'DEPARTMENT' AND department_id IS NOT NULL)",
            name="ck_spaces_owner",
        ),
        CheckConstraint("version >= 1", name="ck_spaces_version"),
        CheckConstraint("length(trim(name)) > 0", name="ck_spaces_name"),
        CheckConstraint("length(trim(location)) > 0", name="ck_spaces_location"),
    )

    space_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[str] = mapped_column(Text, nullable=False)
    is_study_room: Mapped[bool] = mapped_column(Boolean, nullable=False)
    owner_kind: Mapped[str] = mapped_column(Text, nullable=False)
    department_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("departments.department_id"), nullable=True, index=True
    )
    registered: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class SystemSubject(Base):
    """后台模块的授权主体；模块身份仍由共同凭据校验确定。"""

    __tablename__ = "system_subjects"
    __table_args__ = (
        CheckConstraint("module_id IN ('m1', 'm2', 'm3')", name="ck_system_subjects_module"),
        CheckConstraint("status IN ('ACTIVE', 'DISABLED')", name="ck_system_subjects_status"),
    )

    module_id: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="ACTIVE")


class SystemGrant(Base):
    """SYSTEM 模式的持久化授权，与用户角色授权分开保存。"""

    __tablename__ = "system_grants"
    __table_args__ = (
        CheckConstraint(
            "(scope_type = 'GLOBAL' AND scope_id IS NULL) OR "
            "(scope_type IN ('DEPARTMENT', 'SPACE') "
            "AND scope_id IS NOT NULL AND length(trim(scope_id)) > 0)",
            name="ck_system_grants_scope",
        ),
        UniqueConstraint(
            "module_id", "permission_code", "scope_type", "scope_id",
            name="uq_system_grants_scope",
            postgresql_nulls_not_distinct=True,
        ),
    )

    grant_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    module_id: Mapped[str] = mapped_column(
        Text, ForeignKey("system_subjects.module_id"), nullable=False
    )
    permission_code: Mapped[str] = mapped_column(
        Text, ForeignKey("permissions.code"), nullable=False
    )
    scope_type: Mapped[str] = mapped_column(Text, nullable=False)
    scope_id: Mapped[str | None] = mapped_column(Text, nullable=True)


class Seat(Base):
    """统一座位主数据；注销保留记录及其座位号。"""

    __tablename__ = "seats"
    __table_args__ = (
        CheckConstraint("length(trim(seat_id)) > 0", name="ck_seats_id"),
        CheckConstraint("length(trim(seat_code)) > 0", name="ck_seats_code"),
        CheckConstraint("version >= 1", name="ck_seats_version"),
        # 两种标签均可省、可组合且不得重复；JSON 数组顺序不作要求。
        CheckConstraint(
            "socket_tags::jsonb IN ('[]'::jsonb, '[\"FIXED\"]'::jsonb, "
            "'[\"RAIL\"]'::jsonb, '[\"FIXED\", \"RAIL\"]'::jsonb, "
            "'[\"RAIL\", \"FIXED\"]'::jsonb)",
            name="ck_seats_socket_tags",
        ),
        UniqueConstraint("seat_code", name="uq_seats_seat_code"),
    )

    seat_id: Mapped[str] = mapped_column(Text, primary_key=True)
    seat_code: Mapped[str] = mapped_column(Text, nullable=False)
    space_id: Mapped[str] = mapped_column(
        Text, ForeignKey("spaces.space_id"), nullable=False, index=True
    )
    registered: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    socket_tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, server_default="[]")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class PublicConfig(Base):
    """单份公共设置；只保存公开参数，不存储服务秘密。"""

    __tablename__ = "public_config"
    __table_args__ = (
        CheckConstraint("config_id = 1", name="ck_public_config_singleton"),
        CheckConstraint("auth_method = 'PASSWORD'", name="ck_public_config_auth"),
        CheckConstraint("notification_channel = 'EMAIL'", name="ck_public_config_channel"),
        CheckConstraint("notification_retry_limit BETWEEN 0 AND 5", name="ck_public_config_retry"),
        CheckConstraint("version >= 1", name="ck_public_config_version"),
    )

    config_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    auth_method: Mapped[str] = mapped_column(Text, nullable=False)
    notification_channel: Mapped[str] = mapped_column(Text, nullable=False)
    notification_retry_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class PublicConfigRequest(Base):
    """仅用于 M1 的 manage_public_config 请求，成功结果随配置原子保存。"""

    __tablename__ = "public_config_requests"
    __table_args__ = (
        CheckConstraint("length(trim(request_id)) > 0", name="ck_public_config_requests_id"),
    )

    request_id: Mapped[str] = mapped_column(Text, primary_key=True)
    input_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    response_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    requested_by_user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.user_id"), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Notification(Base):
    """一个已受理通知；输入快照与受理时重试上限供幂等和后续投递使用。"""

    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint('length(trim(notification_id)) > 0', name='ck_notifications_id'),
        CheckConstraint('length(trim(request_id)) > 0', name='ck_notifications_request'),
        CheckConstraint('length(trim(business_id)) > 0', name='ck_notifications_business'),
        CheckConstraint("source_module IN ('M1', 'M2', 'M3')", name='ck_notifications_source'),
        CheckConstraint("(source_module = 'M1' AND kind IN ('M1_BEFORE_START', 'M1_NOT_CHECKED_IN', 'M1_AUTO_CANCELLED', 'M1_MAINTENANCE_CANCELLED')) OR (source_module = 'M2' AND kind IN ('M2_APPROVAL_PENDING', 'M2_APPROVED', 'M2_REJECTED', 'M2_TIMEOUT_CANCELLED', 'M2_CANCELLED')) OR (source_module = 'M3' AND kind IN ('M3_ASSIGNED', 'M3_REPAIR_STARTED', 'M3_TIMEOUT_ESCALATED', 'M3_ACCEPTANCE_PENDING', 'M3_ACCEPTANCE_RESULT', 'M3_CANCELLED'))", name='ck_notifications_kind'),
        CheckConstraint("channel = 'EMAIL'", name='ck_notifications_channel'),
        CheckConstraint('length(trim(title)) > 0 AND length(title) <= 2000 AND position(chr(10) in title) = 0 AND position(chr(13) in title) = 0', name='ck_notifications_title'),
        CheckConstraint('length(trim(body)) > 0 AND length(body) <= 2000', name='ck_notifications_body'),
        CheckConstraint('retry_limit BETWEEN 0 AND 5', name='ck_notifications_retry'),
        CheckConstraint("jsonb_typeof(input_data::jsonb) = 'object'", name='ck_notifications_input'),
        UniqueConstraint("source_module", "request_id", name="uq_notifications_source_request"),
    )

    notification_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_module: Mapped[str] = mapped_column(Text, nullable=False)
    request_id: Mapped[str] = mapped_column(Text, nullable=False)
    business_id: Mapped[str] = mapped_column(Text, nullable=False)
    space_id: Mapped[str | None] = mapped_column(Text, ForeignKey("spaces.space_id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retry_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    input_data: Mapped[dict] = mapped_column(JSON, nullable=False)


class NotificationDelivery(Base):
    """每个收件人的持久投递记录，区分未尝试、处理中、渠道接受和未知结果。"""

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        CheckConstraint('recipient_order >= 0', name='ck_notification_deliveries_order'),
        CheckConstraint("status IN ('PENDING', 'SENDING', 'SUBMITTED', 'FAILED', 'UNKNOWN')", name='ck_notification_deliveries_status'),
        CheckConstraint('attempt_count BETWEEN 0 AND 6', name='ck_notification_deliveries_count'),
        CheckConstraint('(attempt_count = 0 AND last_attempt_at IS NULL) OR (attempt_count > 0 AND last_attempt_at IS NOT NULL)', name='ck_notification_deliveries_attempt'),
        CheckConstraint("status NOT IN ('SENDING', 'SUBMITTED', 'UNKNOWN') OR attempt_count > 0", name='ck_notification_deliveries_attempted_status'),
        CheckConstraint("(status = 'PENDING' AND next_attempt_at IS NOT NULL) OR (status <> 'PENDING' AND next_attempt_at IS NULL)", name='ck_notification_deliveries_next'),
        CheckConstraint("(status = 'SENDING' AND attempt_token IS NOT NULL AND length(trim(attempt_token)) > 0 AND lease_expires_at IS NOT NULL AND lease_expires_at > last_attempt_at) OR (status <> 'SENDING' AND attempt_token IS NULL AND lease_expires_at IS NULL)", name='ck_notification_deliveries_lease'),
        CheckConstraint('recipient_email IS NULL OR (length(trim(recipient_email)) > 0 AND position(chr(10) in recipient_email) = 0 AND position(chr(13) in recipient_email) = 0)', name='ck_notification_deliveries_email'),
        CheckConstraint("status NOT IN ('SENDING', 'SUBMITTED', 'UNKNOWN') OR recipient_email IS NOT NULL", name='ck_notification_deliveries_address'),
        CheckConstraint('length(trim(reason)) > 0', name='ck_notification_deliveries_reason'),
        UniqueConstraint("notification_id", "recipient_order", name="uq_notification_deliveries_order"),
        Index("ix_notification_deliveries_due", "status", "next_attempt_at"),
        Index("ix_notification_deliveries_lease", "status", "lease_expires_at"),
    )

    notification_id: Mapped[str] = mapped_column(Text, ForeignKey("notifications.notification_id"), primary_key=True)
    recipient_user_id: Mapped[str] = mapped_column(Text, ForeignKey("users.user_id"), primary_key=True)
    recipient_order: Mapped[int] = mapped_column(Integer, nullable=False)
    recipient_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)


class AuditEvent(Base):
    """D-S11审计事件；只经业务服务追加，用户引用与来源核验责任分别保存。"""

    __tablename__ = 'audit_events'
    __table_args__ = (
        CheckConstraint('length(trim(audit_id)) > 0', name='ck_audit_events_id'),
        CheckConstraint('length(trim(request_id)) > 0', name='ck_audit_events_request'),
        CheckConstraint("source_module IN ('M1','M2','M3','D')", name='ck_audit_events_source'),
        CheckConstraint("actor_type IN ('USER','SYSTEM','UNKNOWN')", name='ck_audit_events_actor'),
        CheckConstraint("(actor_type = 'USER' AND actor_user_id IS NOT NULL) OR (actor_type <> 'USER' AND actor_user_id IS NULL)", name='ck_audit_events_actor_user'),
        CheckConstraint('length(trim(object_type)) > 0 AND length(object_type) <= 2000', name='ck_audit_events_object'),
        CheckConstraint('length(trim(business_id)) > 0', name='ck_audit_events_business'),
        CheckConstraint('length(trim(action)) > 0 AND length(action) <= 2000', name='ck_audit_events_action'),
        CheckConstraint("result IN ('SUCCESS','REJECTED','FAILED','UNKNOWN')", name='ck_audit_events_result'),
        CheckConstraint('length(trim(summary)) > 0 AND length(summary) <= 2000', name='ck_audit_events_summary'),
        CheckConstraint("verified_by = source_module OR (verified_by = 'D' AND actor_type = 'USER')", name='ck_audit_events_verified'),
        CheckConstraint("jsonb_typeof(input_data::jsonb) = 'object'", name='ck_audit_events_input'),
        UniqueConstraint('source_module', 'request_id', name='uq_audit_events_source_request'),
        Index('ix_audit_events_time_id', 'occurred_at', 'audit_id'),
        Index('ix_audit_events_source_time', 'source_module', 'occurred_at', 'audit_id'),
        Index('ix_audit_events_business', 'business_id'),
    )

    audit_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_module: Mapped[str] = mapped_column(Text, nullable=False)
    request_id: Mapped[str] = mapped_column(Text, nullable=False)
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(Text, ForeignKey('users.user_id'), nullable=True, index=True)
    object_type: Mapped[str] = mapped_column(Text, nullable=False)
    business_id: Mapped[str] = mapped_column(Text, nullable=False)
    space_id: Mapped[str | None] = mapped_column(Text, ForeignKey('spaces.space_id'), nullable=True, index=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    result: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    verified_by: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    input_data: Mapped[dict] = mapped_column(JSON, nullable=False)


class RoleRequest(Base):
    """(m1, manage_roles, requestId)的成功输入和结果，与授权变更同事务保存。"""

    __tablename__ = 'role_requests'
    __table_args__ = (
        CheckConstraint('length(trim(request_id)) > 0', name='ck_role_requests_id'),
        CheckConstraint("jsonb_typeof(input_data::jsonb) = 'object'", name='ck_role_requests_input'),
        CheckConstraint("jsonb_typeof(response_data::jsonb) = 'object'", name='ck_role_requests_response'),
    )

    request_id: Mapped[str] = mapped_column(Text, primary_key=True)
    input_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    response_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    requested_by_user_id: Mapped[str] = mapped_column(Text, ForeignKey('users.user_id'), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MasterRequest(Base):
    """(m1, maintain_master, requestId)原输入和结果，与主数据同事务。"""
    __tablename__ = 'master_requests'
    __table_args__ = (
        CheckConstraint('length(trim(request_id)) > 0', name='ck_master_requests_id'),
        CheckConstraint("jsonb_typeof(input_data::jsonb) = 'object'", name='ck_master_requests_input'),
        CheckConstraint("jsonb_typeof(response_data::jsonb) = 'object'", name='ck_master_requests_response'),
    )
    request_id: Mapped[str] = mapped_column(Text, primary_key=True)
    input_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    response_data: Mapped[dict] = mapped_column(JSON, nullable=False)
    requested_by_user_id: Mapped[str] = mapped_column(Text, ForeignKey('users.user_id'), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SeatCodeHistory(Base):
    """永久占号集合；无需指向当前座位或随主记录级联删除。"""
    __tablename__ = 'seat_code_history'
    __table_args__ = (CheckConstraint('length(trim(seat_code)) > 0', name='ck_seat_code_history_code'),)
    seat_code: Mapped[str] = mapped_column(Text, primary_key=True)
