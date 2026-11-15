from enum import StrEnum
from typing import Any
from uuid import UUID


class DenyReason(StrEnum):
    """Причины отказа. Значения используются в логах и аудите."""

    USER_NOT_FOUND = "USER_NOT_FOUND"
    USER_BLOCKED = "USER_BLOCKED"
    USER_NOT_ACTIVE = "USER_NOT_ACTIVE"
    ROLE_DENIED = "ROLE_DENIED"
    CATEGORY_NOT_FOUND = "CATEGORY_NOT_FOUND"
    CATEGORY_DENIED = "CATEGORY_DENIED"
    RECORD_NOT_FOUND = "RECORD_NOT_FOUND"
    RECORD_DELETED = "RECORD_DELETED"
    RECORD_DENIED = "RECORD_DENIED"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    FILE_DENIED = "FILE_DENIED"
    FILE_DELETED = "FILE_DELETED"
    FILE_NOT_CLEAN = "FILE_NOT_CLEAN"
    UNKNOWN_TARGET = "UNKNOWN_TARGET"


class PermissionDenied(Exception):
    """Доступ запрещён. Для отображения пользователю используйте explain()."""

    def __init__(
        self,
        reason: DenyReason | str,
        user_id: UUID | None = None,
        object_type: str | None = None,
        object_id: str | None = None,
    ) -> None:
        self.reason = reason
        self.user_id = user_id
        self.object_type = object_type
        self.object_id = object_id
        super().__init__(f"Permission denied: {reason}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": str(self.reason),
            "user_id": str(self.user_id) if self.user_id else None,
            "object_type": self.object_type,
            "object_id": self.object_id,
        }


class UserBlocked(PermissionDenied):
    """Пользователь заблокирован и не может выполнять никакие операции."""


class UserNotFound(PermissionDenied):
    """Пользователь не зарегистрирован в системе."""
