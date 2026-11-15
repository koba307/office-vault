import csv
from io import StringIO

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import Record


async def export_records_csv(session: AsyncSession, category_code: str | None = None) -> str:
    q = select(Record).where(Record.status == "ACTIVE").options(selectinload(Record.fields))
    if category_code:
        from app.services.records.service import get_category_by_code
        cat = await get_category_by_code(session, category_code)
        if cat:
            q = q.where(Record.category_id == cat.id)
    result = await session.execute(q)
    records = result.scalars().all()

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "title", "type", "description", "fields"])
    for r in records:
        fields_str = "; ".join(f"{f.field_key}={f.value_text or '***'}" for f in r.fields)
        writer.writerow([str(r.id), r.title, r.record_type.value, r.description or "", fields_str])
    return output.getvalue()
