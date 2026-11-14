from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.encryption import encrypt_secret
from app.database.models import Category, Record, RecordField, RecordType, User


async def get_record(session: AsyncSession, record_id: UUID) -> Record | None:
    result = await session.execute(
        select(Record).where(Record.id == record_id).options(selectinload(Record.fields))
    )
    return result.scalar_one_or_none()


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
