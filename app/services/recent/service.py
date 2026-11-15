from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import Category, Record
from app.services.auth.permissions import user_can_access_category


async def list_recent_records(session: AsyncSession, user, limit: int = 10) -> list[Record]:
    # Fetch recent records, then filter by category access
    result = await session.execute(
        select(Record)
        .where(Record.status == "ACTIVE")
        .options(selectinload(Record.fields))
        .order_by(Record.updated_at.desc())
        .limit(limit * 3)
    )
    records = result.scalars().all()

    allowed = []
    for r in records:
        cat = await session.get(Category, r.category_id)
        if not cat:
            continue
        if await user_can_access_category(session, user, cat.code):
            allowed.append(r)
            if len(allowed) >= limit:
                break
    return allowed
