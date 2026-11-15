from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import Integer, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AccessMode,
    Category,
    CategoryGroupPermission,
    CategoryPermission,
    CategoryUserPermission,
    GroupMember,
    RecordGroupPermission,
    RecordPermission,
    RecordUserPermission,
    SystemSetting,
)

# Версии хранятся в system_settings, чтобы их можно было читать и менять в той же транзакции.
GLOBAL_VERSION_KEY = "permissions:global_version"
USER_VERSION_KEY_PREFIX = "permissions:user_version:"

# Режимы, которые затрагивают всех сотрудников сразу
BROAD_MODES = (AccessMode.ALL_EMPLOYEES, AccessMode.BOSSES_ONLY)


def user_version_key(user_id: UUID) -> str:
    return f"{USER_VERSION_KEY_PREFIX}{user_id}"


def _version_of(value: dict | None) -> int:
    return int((value or {}).get("v", 0))


async def read_versions(session: AsyncSession, user_id: UUID) -> dict[str, int]:
    """Возвращает версию пользователя и глобальную версию одним запросом."""
    user_key = user_version_key(user_id)
    result = await session.execute(
        select(SystemSetting.key, SystemSetting.value).where(
            SystemSetting.key.in_([user_key, GLOBAL_VERSION_KEY])
        )
    )
    values = {key: _version_of(value) for key, value in result.all()}
    return {"user": values.get(user_key, 0), "global": values.get(GLOBAL_VERSION_KEY, 0)}


async def _bump(session: AsyncSession, key: str) -> None:
    # Атомарный инкремент через upsert, без гонок между воркерами и ботом
    current = func.coalesce(SystemSetting.value["v"].astext.cast(Integer), 0)
    stmt = (
        pg_insert(SystemSetting)
        .values(key=key, value={"v": 1})
        .on_conflict_do_update(
            index_elements=[SystemSetting.key],
            set_={"value": func.jsonb_build_object("v", current + 1), "updated_at": func.now()},
        )
    )
    await session.execute(stmt)


async def get_user_version(user_id: UUID, session: AsyncSession) -> int:
    result = await session.execute(
        select(SystemSetting.value).where(SystemSetting.key == user_version_key(user_id))
    )
    return _version_of(result.scalar_one_or_none())


async def bump_user_version(user_id: UUID, session: AsyncSession) -> None:
    await _bump(session, user_version_key(user_id))


async def bump_global_version(session: AsyncSession) -> None:
    await _bump(session, GLOBAL_VERSION_KEY)


async def bump_object_version(
    target_type: str, target_id: UUID, session: AsyncSession
) -> set[UUID] | None:
    """Инкрементирует версии всех пользователей, чьи права зависят от объекта.

    Возвращает затронутых пользователей или None, если затронуты все сотрудники
    (тогда версия глобальная).
    """
    if target_type == "category":
        owner_ids = await _category_subtree(session, target_id)
        result = await session.execute(
            select(CategoryPermission.access_mode).where(
                CategoryPermission.category_id.in_(owner_ids)
            )
        )
        return await _affected_users(
            session,
            result.scalars().all(),
            CategoryGroupPermission,
            CategoryUserPermission,
            "category_id",
            owner_ids,
        )
    if target_type == "record":
        result = await session.execute(
            select(RecordPermission.access_mode).where(RecordPermission.record_id == target_id)
        )
        mode = result.scalar_one_or_none()
        # Запись без собственного RecordPermission наследует категорию, её версии не меняются
        if mode is None:
            return set()
        return await _affected_users(
            session,
            [mode],
            RecordGroupPermission,
            RecordUserPermission,
            "record_id",
            [target_id],
        )
    raise ValueError(f"Неизвестный тип объекта для версий прав: {target_type}")


async def _category_subtree(session: AsyncSession, category_id: UUID) -> list[UUID]:
    # Подкатегории наследуют права родителя, поэтому учитываем всё поддерево
    tree = (
        select(Category.id).where(Category.id == category_id).cte("category_tree", recursive=True)
    )
    tree = tree.union_all(select(Category.id).join(tree, Category.parent_id == tree.c.id))
    result = await session.execute(select(tree.c.id))
    return list(result.scalars().all())


async def _affected_users(
    session: AsyncSession,
    modes: Sequence[AccessMode],
    group_model: type,
    user_model: type,
    owner_column: str,
    owner_ids: list[UUID],
) -> set[UUID] | None:
    if any(mode in BROAD_MODES for mode in modes):
        await bump_global_version(session)
        return None

    owner = getattr(group_model, owner_column)
    group_result = await session.execute(
        select(group_model.group_id).where(owner.in_(owner_ids), group_model.granted.is_(True))
    )
    group_ids = list(group_result.scalars().all())

    affected: set[UUID] = set()
    if group_ids:
        member_result = await session.execute(
            select(GroupMember.user_id).where(GroupMember.group_id.in_(group_ids))
        )
        affected.update(member_result.scalars().all())

    direct_result = await session.execute(
        select(user_model.user_id).where(getattr(user_model, owner_column).in_(owner_ids))
    )
    affected.update(direct_result.scalars().all())

    for user_id in affected:
        await bump_user_version(user_id, session)
    return affected
