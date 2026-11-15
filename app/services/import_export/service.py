import csv
import io

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import Record, RecordType, User
from app.services.records.service import create_record


async def import_records_csv(
    session: AsyncSession,
    *,
    category_code: str,
    csv_text: str,
    created_by: User,
) -> int:
    reader = csv.DictReader(io.StringIO(csv_text))
    count = 0
    for row in reader:
        title = row.get("title") or "Без названия"
        rtype_raw = (row.get("type") or "OTHER").upper()
        try:
            rtype = RecordType(rtype_raw)
        except ValueError:
            rtype = RecordType.OTHER
        description = row.get("description") or None

        fields = {}
        secret_fields = set()
        fields_raw = row.get("fields") or ""
        for part in fields_raw.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = key.strip()
            value = value.strip()
            if value == "***":
                continue
            fields[key] = value
            if "password" in key.lower() or "secret" in key.lower():
                secret_fields.add(key)

        await create_record(
            session,
            category_code=category_code,
            record_type=rtype,
            title=title,
            description=description,
            created_by=created_by,
            fields=fields,
            secret_fields=secret_fields,
        )
        count += 1
    return count


async def export_records_csv(session: AsyncSession, category_code: str | None = None) -> str:
    q = select(Record).where(Record.status == "ACTIVE").options(selectinload(Record.fields))
    if category_code:
        from app.services.records.service import get_category_by_code

        cat = await get_category_by_code(session, category_code)
        if cat:
            q = q.where(Record.category_id == cat.id)
    result = await session.execute(q)
    records = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "title", "type", "description", "fields"])
    for r in records:
        fields_str = "; ".join(f"{f.field_key}={f.value_text or '***'}" for f in r.fields)
        writer.writerow([str(r.id), r.title, r.record_type.value, r.description or "", fields_str])
    return output.getvalue()