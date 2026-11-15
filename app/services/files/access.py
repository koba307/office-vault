"""Права доступа к файлам и журнал обращений к ним.

Решения принимает PermissionService: здесь только обёртки под действия с файлами.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Category, FileAccessLog, PermissionCode, User
from app.services.permissions.category import resolve_category_id
from app.services.permissions.service import TARGET_CATEGORY, TARGET_FILE, PermissionService

ACCESS_SUCCESS = "SUCCESS"
ACCESS_DENIED = "DENIED"
ACCESS_ERROR = "ERROR"


async def check_file_access(user: User | None, file_id: UUID | str, session: AsyncSession) -> bool:
    """Право просмотра файла: доступна хотя бы одна связанная запись."""
    return await PermissionService.can(user, PermissionCode.VIEW, TARGET_FILE, file_id, session)


async def check_file_download(
    user: User | None, file_id: UUID | str, session: AsyncSession
) -> bool:
    """Право DOWNLOAD с проверками скана (CLEAN) и удаления — они внутри PermissionService."""
    return await PermissionService.can(user, PermissionCode.DOWNLOAD, TARGET_FILE, file_id, session)


async def check_file_upload(user: User | None, category_code: str, session: AsyncSession) -> bool:
    """Право UPLOAD в категорию, которая должна быть активна."""
    category_id = await resolve_category_id(session, category_code)
    if category_id is None:
        return False
    category = await session.get(Category, category_id)
    if category is None or category.status != "ACTIVE":
        return False
    return await PermissionService.can(
        user, PermissionCode.UPLOAD, TARGET_CATEGORY, category_id, session
    )


async def log_file_access(
    file_id: UUID,
    user_id: UUID | None,
    action: str,
    result: str,
    session: AsyncSession,
) -> None:
    """Пишет обращение к файлу. Коммит нужен, чтобы запись о отказе сохранилась."""
    session.add(FileAccessLog(file_id=file_id, user_id=user_id, action=action, result=result))
    await session.commit()
