
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

# Движок для служебных нужд (миграции и т.п.). Создаётся один раз при импорте.
engine = create_async_engine(settings.database_url, echo=False)


class _SessionContext:
    """Асинхронный контекст-менеджер, создающий отдельный движок на сессию.

    Изоляция движка по сессии убирает привязку пула соединений к event loop,
    из-за которой при прогоне тестов возникало ``Future attached to a different loop``.
    """

    def __init__(self) -> None:
        self._engine = create_async_engine(settings.database_url, echo=False)
        self._factory = async_sessionmaker(self._engine, class_=AsyncSession, expire_on_commit=False)
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> AsyncSession:
        self._session = self._factory()
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            await self._session.close()
        await self._engine.dispose()


def async_session() -> _SessionContext:
    return _SessionContext()
