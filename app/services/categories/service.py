import re
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Category, Record

# Статусы, которые допускает управление категориями.
CATEGORY_STATUSES = frozenset({"ACTIVE", "DISABLED", "ARCHIVED"})

# Корневые разделы из ТЗ нельзя удалять, переименовывать или архивировать.
SYSTEM_CATEGORY_CODES = frozenset({"holodka", "second_stage", "closers", "general"})

# Код участвует в callback_data, поэтому ограничиваем алфавит.
_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")


class CategoryError(ValueError):
    """Базовая ошибка домена категорий."""


class CategoryNotFound(CategoryError):
    """Категория с указанным кодом или ID не найдена."""


class CategoryConflict(CategoryError):
    """Категория с таким кодом уже существует."""


class CategoryValidationError(CategoryError):
    """Некорректные данные категории."""


class CategoryInUseError(CategoryError):
    """Категорию нельзя удалить: есть вложенные категории или записи."""


def validate_category_code(code: str) -> str:
    """Нормализует и проверяет код категории."""
    normalized = (code or "").strip().lower()
    if not _CODE_RE.match(normalized):
        raise CategoryValidationError(
            "Код должен содержать 2–32 символа: строчные латинские буквы, цифры, «_» или «-»."
        )
    return normalized


def validate_category_name(name: str) -> str:
    """Проверяет отображаемое имя категории."""
    normalized = (name or "").strip()
    if not normalized:
        raise CategoryValidationError("Название категории не может быть пустым.")
    if len(normalized) > 128:
        raise CategoryValidationError("Название категории не может быть длиннее 128 символов.")
    return normalized


def _as_uuid(value: UUID | str | None) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _validate_status(status: str) -> str:
    normalized = (status or "").strip().upper()
    if normalized not in CATEGORY_STATUSES:
        allowed = ", ".join(sorted(CATEGORY_STATUSES))
        raise CategoryValidationError(f"Недопустимый статус категории. Допустимо: {allowed}.")
    return normalized


def _is_system_category(category: Category) -> bool:
    """Системные разделы защищены от переименования, архивации и удаления."""
    return category.parent_id is None and category.code in SYSTEM_CATEGORY_CODES


async def list_categories(
    session: AsyncSession,
    parent_id: UUID | str | None = None,
    *,
    include_archived: bool = False,
) -> list[Category]:
    """Возвращает корневые категории или подкатегории указанной категории."""
    resolved_parent = _as_uuid(parent_id)
    if parent_id is not None and resolved_parent is None:
        raise CategoryValidationError("Некорректный ID родительской категории.")

    stmt = select(Category)
    if parent_id is None:
        stmt = stmt.where(Category.parent_id.is_(None))
    else:
        stmt = stmt.where(Category.parent_id == resolved_parent)
    if not include_archived:
        stmt = stmt.where(Category.status == "ACTIVE")
    stmt = stmt.order_by(Category.sort_order, Category.name)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_category(session: AsyncSession, code: str) -> Category | None:
    """Ищет категорию по уникальному коду."""
    normalized = (code or "").strip().lower()
    if not normalized:
        return None
    result = await session.execute(select(Category).where(Category.code == normalized))
    return result.scalar_one_or_none()


async def get_category_by_id(
    session: AsyncSession, category_id: UUID | str
) -> Category | None:
    """Ищет категорию по ID; некорректный ID считается отсутствующей категорией."""
    resolved = _as_uuid(category_id)
    if resolved is None:
        return None
    return await session.get(Category, resolved)


async def create_category(
    session: AsyncSession,
    code: str,
    name: str,
    parent_id: UUID | str | None = None,
    category_type: str | None = None,
    sort_order: int | None = None,
) -> Category:
    """Создаёт категорию или подкатегорию и фиксирует транзакцию."""
    normalized_code = validate_category_code(code)
    normalized_name = validate_category_name(name)
    resolved_parent = _as_uuid(parent_id)
    if parent_id is not None and resolved_parent is None:
        raise CategoryValidationError("Некорректный ID родительской категории.")

    existing = await get_category(session, normalized_code)
    if existing is not None:
        raise CategoryConflict(f"Категория с кодом «{normalized_code}» уже существует.")

    if resolved_parent is not None:
        parent = await session.get(Category, resolved_parent)
        if parent is None:
            raise CategoryNotFound("Родительская категория не найдена.")
        if parent.status != "ACTIVE":
            raise CategoryValidationError("Родительская категория недоступна.")

    type_name = (category_type or ("subcategory" if resolved_parent else "root")).strip().lower()
    if not type_name or len(type_name) > 32:
        raise CategoryValidationError("Некорректный тип категории.")

    order = sort_order
    if order is None:
        order = await _next_sort_order(session, resolved_parent)
    if not isinstance(order, int):
        raise CategoryValidationError("Порядок сортировки должен быть целым числом.")

    category = Category(
        code=normalized_code,
        name=normalized_name,
        parent_id=resolved_parent,
        category_type=type_name,
        sort_order=order,
        status="ACTIVE",
    )
    session.add(category)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise CategoryConflict(
            f"Категория с кодом «{normalized_code}» уже существует."
        ) from exc
    await session.refresh(category)
    return category


async def update_category(
    session: AsyncSession,
    category_id: UUID | str,
    name: str | None = None,
    sort_order: int | None = None,
    status: str | None = None,
) -> Category:
    """Обновляет имя, порядок сортировки и/или статус категории."""
    category = await get_category_by_id(session, category_id)
    if category is None:
        raise CategoryNotFound("Категория не найдена.")

    if name is not None:
        normalized_name = validate_category_name(name)
        if _is_system_category(category) and normalized_name != category.name:
            raise CategoryValidationError("Системный раздел нельзя переименовать.")
        category.name = normalized_name
    if sort_order is not None:
        if not isinstance(sort_order, int):
            raise CategoryValidationError("Порядок сортировки должен быть целым числом.")
        category.sort_order = sort_order
    if status is not None:
        normalized_status = _validate_status(status)
        if _is_system_category(category) and normalized_status != "ACTIVE":
            raise CategoryValidationError("Системный раздел нельзя архивировать или отключать.")
        category.status = normalized_status

    await session.commit()
    await session.refresh(category)
    return category


async def archive_category(session: AsyncSession, category_id: UUID | str) -> Category:
    """Переводит категорию в архив, не удаляя связанные данные."""
    return await update_category(session, category_id, status="ARCHIVED")


async def count_category_records(
    session: AsyncSession, category_id: UUID | str
) -> int:
    """Считает записи, привязанные к категории как к основной или подкатегории."""
    resolved = _as_uuid(category_id)
    if resolved is None:
        return 0
    result = await session.execute(
        select(func.count())
        .select_from(Record)
        .where(or_(Record.category_id == resolved, Record.subcategory_id == resolved))
    )
    return int(result.scalar_one())


async def delete_category(session: AsyncSession, category_id: UUID | str) -> None:
    """Удаляет пустую категорию. Системные корневые разделы не удаляются."""
    category = await get_category_by_id(session, category_id)
    if category is None:
        raise CategoryNotFound("Категория не найдена.")
    if _is_system_category(category):
        raise CategoryInUseError("Системный раздел нельзя удалить.")

    children = await session.execute(
        select(func.count()).select_from(Category).where(Category.parent_id == category.id)
    )
    if int(children.scalar_one()) > 0:
        raise CategoryInUseError(
            "Сначала удалите или перенесите вложенные подкатегории."
        )

    records = await count_category_records(session, category.id)
    if records > 0:
        raise CategoryInUseError(
            f"В категории есть записи ({records}). Архивируйте её вместо удаления."
        )

    try:
        await session.delete(category)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise CategoryInUseError(
            "Категория используется другими объектами и не может быть удалена."
        ) from exc


async def _next_sort_order(
    session: AsyncSession, parent_id: UUID | None
) -> int:
    stmt = select(func.max(Category.sort_order))
    if parent_id is None:
        stmt = stmt.where(Category.parent_id.is_(None))
    else:
        stmt = stmt.where(Category.parent_id == parent_id)
    result = await session.execute(stmt)
    current = result.scalar_one_or_none()
    return (current or 0) + 1
