import os

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.database.models import (
    Category,
    CategoryGroupPermission,
    Group,
    Permission,
    PermissionCode,
    Role,
    RoleCode,
    RolePermission,
    User,
    UserStatus,
)
from app.database.seed import seed_system_data
from app.services.auth.admin import set_user_role


@pytest.mark.asyncio
async def test_seed_system_data_is_idempotent():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("Set TEST_DATABASE_URL to an isolated PostgreSQL test database")

    engine = create_async_engine(database_url)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        for _ in range(2):
            async with session_factory() as session:
                await seed_system_data(session)

        async with session_factory() as session:
            categories = (await session.execute(select(func.count()).select_from(Category))).scalar_one()
            groups = (await session.execute(select(func.count()).select_from(Group))).scalar_one()
            roles = (await session.execute(select(func.count()).select_from(Role))).scalar_one()
            permissions = (
                await session.execute(select(func.count()).select_from(Permission))
            ).scalar_one()
            category_group_permissions = (
                await session.execute(select(func.count()).select_from(CategoryGroupPermission))
            ).scalar_one()
            role_permissions = (
                await session.execute(select(func.count()).select_from(RolePermission))
            ).scalar_one()
            superadmin = (
                await session.execute(
                    select(User).where(User.telegram_id == settings.superadmin_telegram_id)
                )
            ).scalar_one()
            category_names = dict(
                (await session.execute(select(Category.code, Category.name))).all()
            )
            group_codes = set((await session.execute(select(Group.code))).scalars().all())

        assert categories == 4
        assert groups == 4
        assert category_names == {
            "holodka": "Холодка",
            "second_stage": "2й Этап",
            "closers": "Клоузеры",
            "general": "Общее",
        }
        assert group_codes == {
            "GROUP_HOLODKA",
            "GROUP_SECOND_STAGE",
            "GROUP_CLOSERS",
            "GROUP_GENERAL",
        }
        assert roles == len(RoleCode)
        assert permissions == len(PermissionCode)
        assert category_group_permissions == 4
        assert role_permissions == 32
        assert superadmin.role == RoleCode.SUPERADMIN
        assert superadmin.role_id is not None
        with pytest.raises(ValueError, match="configured SUPERADMIN role cannot be changed"):
            await set_user_role(session, superadmin.id, RoleCode.BOSS)

        roles_by_code = {
            role.code: role
            for role in (await session.execute(select(Role))).scalars().all()
        }
        existing_test_user = (
            await session.execute(select(User).where(User.telegram_id == 9_000_000_001))
        ).scalar_one_or_none()
        if existing_test_user is not None:
            await session.delete(existing_test_user)
            await session.commit()

        employee = User(
            telegram_id=9_000_000_001,
            display_name="Migration test user",
            role=RoleCode.EMPLOYEE,
            role_id=roles_by_code[RoleCode.EMPLOYEE.value].id,
            status=UserStatus.ACTIVE,
        )
        session.add(employee)
        await session.commit()

        with pytest.raises(ValueError, match="SUPERADMIN can only be assigned"):
            await set_user_role(session, employee.id, RoleCode.SUPERADMIN)
        await set_user_role(session, employee.id, RoleCode.BOSS)
        await session.refresh(employee)
        assert employee.role == RoleCode.BOSS
        assert employee.role_id == roles_by_code[RoleCode.BOSS.value].id

        await session.delete(employee)
        await session.commit()
    finally:
        await engine.dispose()
