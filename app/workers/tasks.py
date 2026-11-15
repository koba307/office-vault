import csv
import io
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.database.models import Record
from app.database.session import async_session
from app.services.files.service import get_minio_client


async def backup_records(ctx) -> str:
    """Export all active records to CSV and upload to MinIO as a backup."""
    async with async_session() as session:
        result = await session.execute(
            select(Record).where(Record.status == "ACTIVE").options(selectinload(Record.fields))
        )
        records = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "title", "type", "description", "fields"])
    for r in records:
        fields_str = "; ".join(f"{f.field_key}={f.value_text or '***'}" for f in r.fields)
        writer.writerow([str(r.id), r.title, r.record_type.value, r.description or "", fields_str])

    data = output.getvalue().encode("utf-8")
    filename = f"backups/office_vault_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.csv"

    try:
        client = get_minio_client()
        bucket = settings.minio_bucket
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
        from io import BytesIO

        client.put_object(bucket, filename, BytesIO(data), length=len(data), content_type="text/csv")
        return f"Backup uploaded: {filename}"
    except Exception as e:  # noqa: BLE001
        # Fall back to local file in container
        with open(f"/tmp/{filename.split('/')[-1]}", "wb") as f:  # noqa: ASYNC230
            f.write(data)
        return f"Backup saved locally (MinIO unavailable): {e}"


async def cleanup_archived(ctx) -> str:
    """Hard-delete records archived more than 30 days ago."""
    from datetime import timedelta

    from sqlalchemy import delete

    from app.database.models import Record

    cutoff = datetime.now(UTC) - timedelta(days=30)
    async with async_session() as session:
        await session.execute(
            delete(Record).where(Record.status == "ARCHIVED", Record.updated_at < cutoff)
        )
        await session.commit()
    return "Cleanup done"