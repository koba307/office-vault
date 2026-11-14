import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage

from app.core.config import settings
from app.database.models import Base
from app.database.session import async_session, engine

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables ensured")


async def seed_system_data() -> None:
    """Создаёт 4 системные категории и группы при первом запуске."""
    from app.database.models import Category, Group, RoleCode, User, UserStatus

    async with async_session() as session:
        result = await session.execute(
            Category.__table__.select().where(Category.code == "holodka")
        )
        if result.first():
            logger.info("System categories already exist, skipping seed")
            return

        categories = [
            Category(code="holodka", name="Холодка", category_type="root", sort_order=1),
            Category(code="second_stage", name="2й Этап", category_type="root", sort_order=2),
            Category(code="closers", name="Клоузеры", category_type="root", sort_order=3),
            Category(code="general", name="Общее", category_type="root", sort_order=4),
        ]
        session.add_all(categories)

        groups = [
            Group(code="GROUP_HOLODKA", name="Холодка", is_system=True),
            Group(code="GROUP_SECOND_STAGE", name="2й Этап", is_system=True),
            Group(code="GROUP_CLOSERS", name="Клоузеры", is_system=True),
            Group(code="GROUP_GENERAL", name="Общее", is_system=True),
        ]
        session.add_all(groups)

        superadmin = User(
            telegram_id=settings.superadmin_telegram_id,
            display_name="SUPERADMIN",
            role=RoleCode.SUPERADMIN,
            status=UserStatus.ACTIVE,
        )
        session.add(superadmin)

        await session.commit()
        logger.info("System categories, groups and SUPERADMIN seeded")


async def main() -> None:
    await init_db()
    await seed_system_data()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    storage = RedisStorage.from_url(settings.redis_url)
    dp = Dispatcher(storage=storage)

    from app.bot.handlers import favorites, menu, records, search, start

    dp.include_router(start.router)
    dp.include_router(menu.router)
    dp.include_router(records.router)
    dp.include_router(search.router)
    dp.include_router(favorites.router)

    logger.info("Starting OFFICE VAULT bot...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
