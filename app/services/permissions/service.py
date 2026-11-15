from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Category, PermissionCode, User
from app.services.permissions import versions
from app.services.permissions.category import (
    CATEGORY_ACTIONS,
    category_denial_reason,
    resolve_category_id,
)
from app.services.permissions.context import as_uuid, load_access_context
from app.services.permissions.errors import (
    DenyReason,
    PermissionDenied,
    UserBlocked,
    UserNotFound,
)
from app.services.permissions.file import file_denial_reason
from app.services.permissions.record import record_denial_reason

TARGET_SYSTEM = "system"
TARGET_CATEGORY = "category"
TARGET_RECORD = "record"
TARGET_FILE = "file"

DENY_MESSAGES: dict[DenyReason, str] = {
    DenyReason.USER_NOT_FOUND: "Пользователь не зарегистрирован в системе.",
    DenyReason.USER_BLOCKED: "Пользователь заблокирован.",
    DenyReason.USER_NOT_ACTIVE: "Пользователь ещё не активирован.",
    DenyReason.ROLE_DENIED: "Роль пользователя не содержит нужного права.",
    DenyReason.CATEGORY_NOT_FOUND: "Категория не найдена.",
    DenyReason.CATEGORY_DENIED: "Категория недоступна пользователю.",
    DenyReason.RECORD_NOT_FOUND: "Запись не найдена.",
    DenyReason.RECORD_DELETED: "Запись удалена.",
    DenyReason.RECORD_DENIED: "Запись недоступна пользователю.",
    DenyReason.FILE_NOT_FOUND: "Файл не найден.",
    DenyReason.FILE_DENIED: "Файл недоступен: ни одна связанная запись не разрешает действие.",
    DenyReason.FILE_DELETED: "Файл удалён.",
    DenyReason.FILE_NOT_CLEAN: "Файл не прошёл антивирусную проверку.",
    DenyReason.UNKNOWN_TARGET: "Неизвестный тип объекта.",
}


async def _decide(
    user: User | None,
    action: PermissionCode | str,
    target_type: str | None,
    target_id: UUID | str | None,
    session: AsyncSession,
) -> DenyReason | None:
    ctx, reason = await load_access_context(user, session)
    if reason is not None or ctx is None:
        return reason

    permission = PermissionCode(action)
    if target_type in (None, TARGET_SYSTEM):
        return None if ctx.has_permission(permission) else DenyReason.ROLE_DENIED

    if target_type == TARGET_CATEGORY:
        category_id = await resolve_category_id(session, target_id)
        if category_id is None:
            return DenyReason.CATEGORY_NOT_FOUND
        return await category_denial_reason(ctx, category_id, permission, session)

    if target_type == TARGET_RECORD:
        record_id = as_uuid(target_id)
        if record_id is None:
            return DenyReason.RECORD_NOT_FOUND
        return await record_denial_reason(ctx, record_id, permission, session)

    if target_type == TARGET_FILE:
        file_id = as_uuid(target_id)
        if file_id is None:
            return DenyReason.FILE_NOT_FOUND
        return await file_denial_reason(ctx, file_id, permission, session)

    return DenyReason.UNKNOWN_TARGET


class PermissionService:
    """Единая точка проверки прав. Методы не коммитят транзакцию вызывающего кода."""

    @staticmethod
    async def can(
        user: User | None,
        action: PermissionCode | str,
        target_type: str | None,
        target_id: UUID | str | None,
        session: AsyncSession,
    ) -> bool:
        return await _decide(user, action, target_type, target_id, session) is None

    @staticmethod
    async def require(
        user: User | None,
        action: PermissionCode | str,
        target_type: str | None,
        target_id: UUID | str | None,
        session: AsyncSession,
    ) -> None:
        reason = await _decide(user, action, target_type, target_id, session)
        if reason is None:
            return

        user_id = getattr(user, "id", None)
        object_id = None if target_id is None else str(target_id)
        if reason == DenyReason.USER_NOT_FOUND:
            raise UserNotFound(
                reason, user_id=user_id, object_type=target_type, object_id=object_id
            )
        if reason == DenyReason.USER_BLOCKED:
            raise UserBlocked(reason, user_id=user_id, object_type=target_type, object_id=object_id)
        raise PermissionDenied(
            reason, user_id=user_id, object_type=target_type, object_id=object_id
        )

    @staticmethod
    async def explain(
        user: User | None,
        action: PermissionCode | str,
        target_type: str | None,
        target_id: UUID | str | None,
        session: AsyncSession,
    ) -> str:
        """Человекочитаемая причина отказа для администратора."""
        reason = await _decide(user, action, target_type, target_id, session)
        if reason is None:
            return "Доступ разрешён."
        return f"{DENY_MESSAGES[reason]} (код: {reason.value})"

    @staticmethod
    async def effective_access(user: User | None, session: AsyncSession) -> list[dict[str, Any]]:
        """Категории, которые видит пользователь, и разрешённые в них действия."""
        ctx, reason = await load_access_context(user, session)
        if reason is not None or ctx is None:
            return []

        result = await session.execute(
            select(Category)
            .where(Category.status == "ACTIVE")
            .order_by(Category.sort_order, Category.code)
        )
        available: list[dict[str, Any]] = []
        for category in result.scalars().all():
            actions: list[str] = []
            for action in CATEGORY_ACTIONS:
                if not ctx.has_permission(action):
                    continue
                if await category_denial_reason(ctx, category.id, action, session) is None:
                    actions.append(action.value)
            if PermissionCode.VIEW.value not in actions:
                continue
            available.append(
                {
                    "id": str(category.id),
                    "code": category.code,
                    "name": category.name,
                    "parent_id": str(category.parent_id) if category.parent_id else None,
                    "actions": actions,
                }
            )
        return available

    @staticmethod
    async def get_user_version(user_id: UUID, session: AsyncSession) -> int:
        return await versions.get_user_version(user_id, session)

    @staticmethod
    async def bump_user_version(user_id: UUID, session: AsyncSession) -> None:
        await versions.bump_user_version(user_id, session)

    @staticmethod
    async def bump_object_version(
        target_type: str, target_id: UUID | str, session: AsyncSession
    ) -> set[UUID] | None:
        """Возвращает затронутых пользователей или None, если затронуты все."""
        if target_type == TARGET_CATEGORY:
            category_id = await resolve_category_id(session, target_id)
            if category_id is None:
                return set()
            return await versions.bump_object_version(target_type, category_id, session)
        if target_type == TARGET_RECORD:
            record_id = as_uuid(target_id)
            if record_id is None:
                return set()
            return await versions.bump_object_version(target_type, record_id, session)
        raise ValueError(f"Неизвестный тип объекта для версий прав: {target_type}")

    @staticmethod
    async def bump_global_version(session: AsyncSession) -> None:
        await versions.bump_global_version(session)
