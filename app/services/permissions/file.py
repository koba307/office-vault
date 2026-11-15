from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import File, PermissionCode, RecordFile, ScanStatus, User
from app.services.permissions.category import category_denial_reason, resolve_category_id
from app.services.permissions.context import UserContext, as_uuid, load_access_context
from app.services.permissions.errors import DenyReason
from app.services.permissions.record import record_denial_reason


async def file_denial_reason(
    ctx: UserContext,
    file_id: UUID,
    action: PermissionCode,
    session: AsyncSession,
) -> DenyReason | None:
    """Возвращает причину отказа по файлу или None, если действие разрешено."""
    file = await session.get(File, file_id)
    if file is None:
        return DenyReason.FILE_NOT_FOUND
    if not ctx.has_permission(action):
        return DenyReason.ROLE_DENIED

    # Файл доступен, если доступна хотя бы одна связанная запись
    result = await session.execute(
        select(RecordFile.record_id).where(RecordFile.file_id == file_id)
    )
    record_ids = list(result.scalars().all())
    if not record_ids:
        # Файл без записи видит только загрузивший его пользователь и суперадмин
        if not (ctx.is_superadmin or file.uploaded_by == ctx.user_id):
            return DenyReason.FILE_DENIED
    else:
        visible = False
        for record_id in record_ids:
            if await record_denial_reason(ctx, record_id, action, session) is None:
                visible = True
                break
        if not visible:
            return DenyReason.FILE_DENIED

    if action == PermissionCode.DOWNLOAD:
        if file.deleted_at is not None:
            return DenyReason.FILE_DELETED
        if file.scan_status != ScanStatus.CLEAN:
            return DenyReason.FILE_NOT_CLEAN
    return None


async def check_file_access(user: User | None, file_id: UUID | str, session: AsyncSession) -> bool:
    ctx, reason = await load_access_context(user, session)
    if reason is not None or ctx is None:
        return False
    resolved = as_uuid(file_id)
    if resolved is None:
        return False
    return await file_denial_reason(ctx, resolved, PermissionCode.VIEW, session) is None


async def check_file_download(
    user: User | None, file_id: UUID | str, session: AsyncSession
) -> bool:
    ctx, reason = await load_access_context(user, session)
    if reason is not None or ctx is None:
        return False
    resolved = as_uuid(file_id)
    if resolved is None:
        return False
    return await file_denial_reason(ctx, resolved, PermissionCode.DOWNLOAD, session) is None


async def check_file_upload(user: User | None, category_code: str, session: AsyncSession) -> bool:
    ctx, reason = await load_access_context(user, session)
    if reason is not None or ctx is None:
        return False
    if not ctx.has_permission(PermissionCode.UPLOAD):
        return False
    category_id = await resolve_category_id(session, category_code)
    if category_id is None:
        return False
    return await category_denial_reason(ctx, category_id, PermissionCode.UPLOAD, session) is None
