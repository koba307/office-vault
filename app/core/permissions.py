"""Pure authorization helpers (no I/O, no database access).

The caller loads the user, group memberships and individual grants from the
database and passes the plain facts into :func:`decide_access`. This keeps the
decision logic deterministic and trivially testable while enforcing Default Deny
(TZ §5.3) on every server-side action.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from app.database.models import AccessMode, RoleCode, UserStatus


class PermissionAction(str, enum.Enum):
    """Operations that can be authorized."""

    VIEW = "VIEW"
    CREATE = "CREATE"
    EDIT = "EDIT"
    DELETE = "DELETE"
    ARCHIVE = "ARCHIVE"
    RESTORE = "RESTORE"
    DOWNLOAD = "DOWNLOAD"
    UPLOAD = "UPLOAD"
    EXPORT = "EXPORT"
    EXPORT_SECRETS = "EXPORT_SECRETS"
    IMPORT = "IMPORT"
    MANAGE_ACCESS = "MANAGE_ACCESS"
    MANAGE_USERS = "MANAGE_USERS"
    MANAGE_GROUPS = "MANAGE_GROUPS"
    MANAGE_CATEGORIES = "MANAGE_CATEGORIES"
    VIEW_AUDIT = "VIEW_AUDIT"
    MANAGE_BACKUP = "MANAGE_BACKUP"
    MANAGE_SYSTEM = "MANAGE_SYSTEM"


class AccessLevel(str, enum.Enum):
    """Result of a role/action lookup."""

    ALLOW = "ALLOW"
    DENY = "DENY"


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    """Outcome of an access check with a human-readable reason for the audit log."""

    result: bool
    reason: str

    def __bool__(self) -> bool:
        return self.result


_EMPLOYEE_ACTIONS = frozenset(
    {
        PermissionAction.VIEW,
        PermissionAction.DOWNLOAD,
    }
)

# BOSS управляет рабочими данными, но не системой, бэкапами, пользователями и группами.
_BOSS_ACTIONS = _EMPLOYEE_ACTIONS | frozenset(
    {
        PermissionAction.CREATE,
        PermissionAction.EDIT,
        PermissionAction.DELETE,
        PermissionAction.ARCHIVE,
        PermissionAction.RESTORE,
        PermissionAction.UPLOAD,
        PermissionAction.EXPORT,
        PermissionAction.EXPORT_SECRETS,
        PermissionAction.IMPORT,
        PermissionAction.MANAGE_ACCESS,
        PermissionAction.MANAGE_CATEGORIES,
        PermissionAction.VIEW_AUDIT,
    }
)

# Матрица ролей из ТЗ §3. SUPERADMIN разрешено всё.
ROLE_PERMISSIONS: dict[RoleCode, frozenset[PermissionAction]] = {
    RoleCode.SUPERADMIN: frozenset(PermissionAction),
    RoleCode.BOSS: _BOSS_ACTIONS,
    RoleCode.EMPLOYEE: _EMPLOYEE_ACTIONS,
}


def _coerce[EnumT: enum.Enum](value: object, enum_cls: type[EnumT]) -> EnumT | None:
    """Приводит строку к enum; неизвестное значение даёт None (Default Deny)."""
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, str):
        try:
            return enum_cls(value.strip().upper())
        except ValueError:
            return None
    return None


def role_access_level(
    role: RoleCode | str | None, action: PermissionAction | str | None
) -> AccessLevel:
    """Return ALLOW/DENY for a role/action pair; unknown input is denied."""
    normalized_role = _coerce(role, RoleCode)
    normalized_action = _coerce(action, PermissionAction)
    if normalized_role is None or normalized_action is None:
        return AccessLevel.DENY

    allowed = ROLE_PERMISSIONS.get(normalized_role, frozenset())
    if normalized_action in allowed:
        return AccessLevel.ALLOW
    return AccessLevel.DENY


def check_role_permission(
    role: RoleCode | str | None, action: PermissionAction | str | None
) -> bool:
    """Check the role matrix from TZ §3. Unknown role or action is denied."""
    return role_access_level(role, action) is AccessLevel.ALLOW


def decide_access(
    access_mode: AccessMode | str | None,
    user_role: RoleCode | str | None,
    user_status: UserStatus | str | None,
    is_superadmin: bool = False,
    is_member_of_group: bool = False,
    has_individual_grant: bool = False,
    has_individual_deny: bool = False,
    granted_by_role: bool = False,
    is_record_owner: bool = False,
) -> PermissionDecision:
    """Resolve access to a category, record or attachment from caller-supplied facts.

    Checks are performed in the order required by TZ §5.3:

    1. the user exists (``user_status`` is not None);
    2. the user is ACTIVE;
    3. the user is not blocked;
    4. the role allows the operation (``granted_by_role``, see
       :func:`check_role_permission`);
    5. group membership or an individual grant exists;
    6. the category is available (``access_mode``);
    7. the record is available (ownership fallback);
    8. the attachment is available (the same function is called with the
       attachment's own access mode).

    ``has_individual_deny`` wins over any allow (deny override). SUPERADMIN is
    the only exception: an active superadmin bypasses access modes because TZ
    §3.1 grants full access. Missing input is always denied (Default Deny).
    """
    mode = _coerce(access_mode, AccessMode)
    role = _coerce(user_role, RoleCode)
    status = _coerce(user_status, UserStatus)

    # Шаг 1. Пользователь должен существовать: None означает «не найден».
    if status is None:
        return PermissionDecision(False, "user not found")

    # Шаг 3. Заблокированный пользователь не проходит дальше ни при каких правах.
    if status is UserStatus.BLOCKED:
        return PermissionDecision(False, "user is blocked")

    # Шаг 2. Только активный пользователь допускается к данным (PENDING тоже deny).
    if status is not UserStatus.ACTIVE:
        return PermissionDecision(False, f"user status is {status.value}")

    # Неизвестный режим доступа — повреждённые данные, поэтому Default Deny.
    if mode is None:
        return PermissionDecision(False, "unknown access mode")

    # Шаг 4. Роль должна быть известна.
    if role is None:
        return PermissionDecision(False, "unknown user role")

    # Суперадмин имеет полный доступ (ТЗ §3.1), если он активен и не заблокирован.
    if is_superadmin or role is RoleCode.SUPERADMIN:
        return PermissionDecision(True, "superadmin bypass")

    if not granted_by_role:
        return PermissionDecision(False, f"role {role.value} is not allowed to perform this action")

    # Deny override: индивидуальный запрет сильнее группового или личного разрешения.
    if has_individual_deny:
        return PermissionDecision(False, "individual deny overrides grants")

    # Шаг 5. Членство в группе либо индивидуальное разрешение.
    group_or_grant = is_member_of_group or has_individual_grant

    # Шаги 6-8. Проверка доступности категории, записи или вложения по режиму.
    if mode is AccessMode.ALL_EMPLOYEES:
        return PermissionDecision(True, "all active employees have access")

    if mode is AccessMode.BOSSES_ONLY:
        if role is RoleCode.BOSS:
            return PermissionDecision(True, "boss access")
        return PermissionDecision(False, "access is restricted to bosses")

    if mode is AccessMode.GROUP:
        if group_or_grant:
            return PermissionDecision(True, "group membership or individual grant")
        # Владелец записи видит свою запись, даже если группа не назначена.
        if is_record_owner:
            return PermissionDecision(True, "record owner")
        return PermissionDecision(False, "no group membership and no individual grant")

    if mode is AccessMode.SELECTED_USERS:
        if has_individual_grant:
            return PermissionDecision(True, "individual grant from selected users")
        if is_record_owner:
            return PermissionDecision(True, "record owner")
        return PermissionDecision(False, "user is not in selected users")

    if mode is AccessMode.PRIVATE:
        if has_individual_grant:
            return PermissionDecision(True, "explicit private assignment")
        if is_record_owner:
            return PermissionDecision(True, "record owner")
        return PermissionDecision(False, "private access requires explicit assignment")

    # Недостижимо для известных режимов, но сохраняет Default Deny при расширении enum.
    return PermissionDecision(False, f"access mode {mode.value} is not implemented")
