from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import Record, RecordField


async def search_records(session: AsyncSession, query: str, limit: int = 20) -> list[Record]:
    q = f"%{query.lower()}%"
    result = await session.execute(
        select(Record)
        .join(RecordField, RecordField.record_id == Record.id, isouter=True)
        .where(
            Record.status == "ACTIVE",
            or_(
                Record.title.ilike(q),
                Record.description.ilike(q),
                RecordField.value_text.ilike(q),
            ),
        )
        .options(selectinload(Record.fields))
        .distinct()
        .limit(limit)
    )
    return list(result.scalars().all())
