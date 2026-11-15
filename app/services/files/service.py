from io import BytesIO
from uuid import UUID, uuid4

from minio import Minio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.database.models import Record, RecordType, User
from app.services.records.service import create_record


def get_minio_client() -> Minio:
    endpoint = settings.minio_endpoint.replace("http://", "").replace("https://", "")
    return Minio(
        endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_endpoint.startswith("https"),
    )


async def upload_file(
    session: AsyncSession,
    *,
    category_code: str,
    title: str,
    description: str | None,
    file_bytes: bytes,
    filename: str,
    content_type: str,
    created_by_id: UUID,
) -> Record:
    client = get_minio_client()
    bucket = getattr(settings, "minio_bucket", None) or "office-vault"
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)

    object_name = f"{uuid4()}/{filename}"
    client.put_object(
        bucket,
        object_name,
        BytesIO(file_bytes),
        length=len(file_bytes),
        content_type=content_type,
    )

    user = await session.get(User, created_by_id)
    if not user:
        raise ValueError("User not found")

    record = await create_record(
        session,
        category_code=category_code,
        record_type=RecordType.FILES,
        title=title or filename,
        description=description,
        created_by=user,
        fields={"filename": filename, "object_name": object_name, "bucket": bucket},
    )
    return record
