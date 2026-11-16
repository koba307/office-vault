import io
import os
import zipfile

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database.models import RoleCode, User
from app.services.max_accounts.service import (
    confirm_issue,
    remaining_max_accounts,
    reserve_max_account,
    upload_max_accounts,
)


@pytest.fixture
async def db_session():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


async def test_upload_and_issue_once(db_session):
    user = (
        await db_session.execute(select(User).where(User.role == RoleCode.SUPERADMIN).limit(1))
    ).scalar_one()
    employee = (
        await db_session.execute(select(User).where(User.role == RoleCode.EMPLOYEE).limit(1))
    ).scalar_one_or_none()
    if employee is None:
        pytest.skip("no employee")

    # Очищаем пул MAX-аккаунтов для изолированности теста.
    await db_session.execute(
        __import__("sqlalchemy").text("DELETE FROM max_account_issues")
    )
    await db_session.execute(
        __import__("sqlalchemy").text("DELETE FROM files WHERE storage_key LIKE 'max_accounts/%'")
    )
    await db_session.commit()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("acc1.txt", "a\n")
        z.writestr("acc2.txt", "b\n")
    count = await upload_max_accounts(db_session, user, buf.getvalue(), "acc.zip")
    assert count == 2
    assert await remaining_max_accounts(db_session) >= 2

    reserved = await reserve_max_account(db_session, employee)
    assert reserved is not None
    file, data = reserved
    assert data in (b"a\n", b"b\n")
    await confirm_issue(db_session, file.id)
    remaining = await remaining_max_accounts(db_session)
    assert remaining >= 1

    # Повторный резерв не должен выдать тот же файл.
    again = await reserve_max_account(db_session, employee)
    if again is not None:
        assert again[0].id != file.id
