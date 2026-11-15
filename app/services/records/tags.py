import re
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Record, RecordTag, Tag
from app.services.records.versions import RecordNotFound

TAG_NAME_MAX_LENGTH = 64

# Короткий ключ тега помещается в callback_data (лимит Telegram — 64 байта).
TAG_KEY_LENGTH = 12

_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class TagError(ValueError):
    """Базовая ошибка домена тегов."""


class TagNotFound(TagError):
    """Тег не найден."""


class TagConflict(TagError):
    """Тег с таким названием уже существует."""


class TagValidationError(TagError):
    """Некорректные данные тега."""


def validate_tag_name(name: str) -> str:
    """Нормализует название тега: единый пробел и нижний регистр, чтобы не плодить дубликаты."""
    normalized = " ".join((name or "").split()).lower()
    if not normalized:
        raise TagValidationError("Название тега не может быть пустым.")
    if len(normalized) > TAG_NAME_MAX_LENGTH:
        raise TagValidationError(
            f"Название тега не может быть длиннее {TAG_NAME_MAX_LENGTH} символов."
        )
    return normalized


def validate_tag_color(color: str | None) -> str | None:
    """Цвет необязателен; если задан, то в формате #RRGGBB."""
    if color is None or not color.strip():
        return None
    normalized = color.strip()
    if not _COLOR_RE.match(normalized):
        raise TagValidationError("Цвет должен быть в формате #RRGGBB.")
    return normalized.lower()


def tag_key(tag: Tag) -> str:
    """Короткий ключ тега для callback_data."""
    return tag.id.hex[:TAG_KEY_LENGTH]


async def create_tag(name: str, color: str | None, session: AsyncSession) -> Tag:
    tag = Tag(name=validate_tag_name(name), color=validate_tag_color(color))
    session.add(tag)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise TagConflict("Тег с таким названием уже существует.") from exc
    await session.refresh(tag)
    return tag


async def list_tags(session: AsyncSession) -> list[Tag]:
    result = await session.execute(select(Tag).order_by(Tag.name))
    return list(result.scalars().all())


async def get_tag(tag_id: UUID, session: AsyncSession) -> Tag | None:
    return await session.get(Tag, tag_id)


async def get_tag_by_name(name: str, session: AsyncSession) -> Tag | None:
    result = await session.execute(select(Tag).where(Tag.name == validate_tag_name(name)))
    return result.scalar_one_or_none()


async def get_tag_by_key(key: str, session: AsyncSession) -> Tag | None:
    """Ищет тег по ключу из callback_data. Неоднозначный ключ считается неизвестным."""
    prefix = (key or "").lower()
    if len(prefix) != TAG_KEY_LENGTH:
        return None
    matches = [tag for tag in await list_tags(session) if tag.id.hex.startswith(prefix)]
    return matches[0] if len(matches) == 1 else None


async def get_record_tags(record_id: UUID, session: AsyncSession) -> list[Tag]:
    result = await session.execute(
        select(Tag)
        .join(RecordTag, RecordTag.tag_id == Tag.id)
        .where(RecordTag.record_id == record_id)
        .order_by(Tag.name)
    )
    return list(result.scalars().all())


async def add_tag_to_record(record_id: UUID, tag_id: UUID, session: AsyncSession) -> bool:
    """Добавляет тег к записи. Возвращает False, если тег уже назначен."""
    record_exists = await session.scalar(select(Record.id).where(Record.id == record_id))
    if record_exists is None:
        raise RecordNotFound("Запись не найдена.")
    if await get_tag(tag_id, session) is None:
        raise TagNotFound("Тег не найден.")
    if await session.get(RecordTag, (record_id, tag_id)) is not None:
        return False

    session.add(RecordTag(record_id=record_id, tag_id=tag_id))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return False
    return True


async def remove_tag_from_record(record_id: UUID, tag_id: UUID, session: AsyncSession) -> bool:
    """Убирает тег у записи. Возвращает False, если связи не было."""
    result = await session.execute(
        delete(RecordTag).where(RecordTag.record_id == record_id, RecordTag.tag_id == tag_id)
    )
    await session.commit()
    return result.rowcount > 0
