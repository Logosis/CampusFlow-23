from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
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