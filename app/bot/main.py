import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage

from app.core.config import settings
from app.database.migrations_runner import run_migrations
from app.database.seed import seed_system_data
from app.database.session import async_session

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)


async def main() -> None:
    await run_migrations()
    async with async_session() as session:
        await seed_system_data(session)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    storage = RedisStorage.from_url(settings.redis_url)
    dp = Dispatcher(storage=storage)

    from app.bot.handlers import (
        admin,
        favorites,
        files,
        menu,
        profile,
        recent,
        records,
        search,
        start,
    )

    dp.include_router(start.router)
    dp.include_router(menu.router)
    dp.include_router(records.router)
    dp.include_router(search.router)
    dp.include_router(favorites.router)
    dp.include_router(admin.router)
    dp.include_router(files.router)
    dp.include_router(recent.router)
    dp.include_router(profile.router)

    logger.info("Starting OFFICE VAULT bot...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
