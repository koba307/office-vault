from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.database.session import async_session
from app.services.auth.permissions import get_user_by_telegram_id, is_user_active
from app.services.search.service import search_records

router = Router()


class Search(StatesGroup):
    waiting_query = State()


@router.callback_query(F.data == "search:start")
async def search_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Search.waiting_query)
    await callback.message.answer("Введите поисковый запрос:")
    await callback.answer()


@router.message(Search.waiting_query)
async def do_search(message: Message, state: FSMContext) -> None:
    query = (message.text or "").strip()
    if not query:
        await message.answer("Пустой запрос.")
        return
    async with async_session() as session:
        user = await get_user_by_telegram_id(session, message.from_user.id)
        if not is_user_active(user):
            await message.answer("Нет доступа")
            await state.clear()
            return
        results = await search_records(session, query)

    if not results:
        await message.answer("Ничего не найдено.")
    else:
        lines = ["🔎 Результаты:\n"]
        for r in results:
            lines.append(f"• {r.title}")
        await message.answer("\n".join(lines))
    await state.clear()
