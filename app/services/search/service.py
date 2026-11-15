from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import Category, Record, RecordField, RecordType


async def search_records(
    session: AsyncSession,
    query: str,
    limit: int = 20,
    *,
    category_code: str | None = None,
    record_type: RecordType | None = None,
) -> list[Record]:
    """Поиск по активным записям.

    category_code ограничивает поиск одним разделом (ТЗ §7.2 «Поиск в разделе»),
    record_type — фильтром по типу данных.
    """
    q = f"%{query.lower()}%"
    conditions = [
        Record.status == "ACTIVE",
        or_(
            Record.title.ilike(q),
            Record.description.ilike(q),
            RecordField.value_text.ilike(q),
        ),
    ]
    if record_type is not None:
        conditions.append(Record.record_type == record_type)

    stmt = (
        select(Record)
        .join(RecordField, RecordField.record_id == Record.id, isouter=True)
        .where(*conditions)
    )
    if category_code is not None:
        stmt = stmt.join(Category, Category.id == Record.category_id).where(
            Category.code == category_code
        )

    result = await session.execute(
        stmt.options(selectinload(Record.fields)).distinct().limit(limit)
    )
    return list(result.scalars().all())
