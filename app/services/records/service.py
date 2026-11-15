from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.encryption import encrypt_secret
from app.database.models import Category, Record, RecordField, RecordType, User


async def update_record(
    session: AsyncSession,
    record_id: UUID,
    *,
    title: str | None = None,
    description: str | None = None,
    user_id: UUID | None = None,
) -> Record | None:
    record = await get_record(session, record_id)
    if not record:
        return None
    if title is not None:
        record.title = title
    if description is not None:
        record.description = description
    record.version = (record.version or 1) + 1
    if user_id:
        record.updated_by = user_id
    await session.commit()
    await session.refresh(record)
    return record


async def get_record(session: AsyncSession, record_id: UUID) -> Record | None:
    result = await session.execute(
        select(Record).where(Record.id == record_id).options(selectinload(Record.fields))
    )
    return result.scalar_one_or_none()


async def archive_record(session: AsyncSession, record_id: UUID, user_id: UUID) -> None:
    record = await get_record(session, record_id)
    if record:
        record.status = "ARCHIVED"
        record.updated_by = user_id
        await session.commit()


async def soft_delete_record(session: AsyncSession, record_id: UUID, user_id: UUID) -> None:
    record = await get_record(session, record_id)
    if record:
        record.status = "DELETED"
        record.updated_by = user_id
        await session.commit()


async def restore_record(session: AsyncSession, record_id: UUID, user_id: UUID) -> None:
    record = await get_record(session, record_id)
    if record and record.status in ("ARCHIVED", "DELETED"):
        record.status = "ACTIVE"
        record.updated_by = user_id
        await session.commit()


async def get_category_by_code(session: AsyncSession, code: str) -> Category | None:
    result = await session.execute(select(Category).where(Category.code == code))
    return result.scalar_one_or_none()


async def list_records(
    session: AsyncSession,
    category_code: str,
    record_type: RecordType,
    limit: int = 20,
) -> list[Record]:
    category = await get_category_by_code(session, category_code)
    if not category:
        return []
    result = await session.execute(
        select(Record)
        .where(Record.category_id == category.id, Record.record_type == record_type, Record.status == "ACTIVE")
        .options(selectinload(Record.fields))
        .order_by(Record.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def list_records_page(
    session: AsyncSession,
    category_code: str,
    *,
    record_type: RecordType | None = None,
    sort: str = "date",
    offset: int = 0,
    limit: int = 10,
) -> tuple[list[Record], int]:
    """Страница активных записей категории и общее количество (для пагинации)."""
    category = await get_category_by_code(session, category_code)
    if not category:
        return [], 0

    conditions = [Record.category_id == category.id, Record.status == "ACTIVE"]
    if record_type is not None:
        conditions.append(Record.record_type == record_type)

    total = await session.scalar(select(func.count()).select_from(Record).where(*conditions))

    order = (
        func.lower(Record.title).asc() if sort == "name" else Record.created_at.desc()
    )
    result = await session.execute(
        select(Record)
        .where(*conditions)
        .options(selectinload(Record.fields))
        .order_by(order, Record.id)
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all()), total or 0


async def create_record(
    session: AsyncSession,
    *,
    category_code: str,
    record_type: RecordType,
    title: str,
    description: str | None,
    created_by: User,
    fields: dict[str, str] | None = None,
    secret_fields: set[str] | None = None,
) -> Record:
    category = await get_category_by_code(session, category_code)
    if not category:
        raise ValueError(f"Category {category_code} not found")

    record = Record(
        category_id=category.id,
        record_type=record_type,
        title=title,
        description=description,
        created_by=created_by.id,
        updated_by=created_by.id,
    )
    session.add(record)
    await session.flush()

    secret_fields = secret_fields or set()
    if fields:
        for idx, (key, value) in enumerate(fields.items()):
            encrypted = None
            value_text = value
            if key in secret_fields and value:
                encrypted = encrypt_secret(value)
                value_text = None
            field = RecordField(
                record_id=record.id,
                field_key=key,
                field_label=key.replace("_", " ").title(),
                field_type="PASSWORD" if key in secret_fields else "TEXT",
                value_text=value_text,
                encrypted_value=encrypted,
                sort_order=idx,
            )
            session.add(field)

    await session.commit()
    await session.refresh(record)
    return record
