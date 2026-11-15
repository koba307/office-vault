from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.handlers.menu import MAIN_TEXT, STALE_TEXT, build_main_menu, open_screen, render_main

# Основной роутер подключается до обработчиков разделов: /cancel должен работать внутри FSM-сценариев
router = Router()
# Ловушка для устаревших callback_data подключается последней
stale_router = Router()


@router.callback_query(F.data == "menu:main")
async def main_menu_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await render_main(callback)


@router.callback_query(F.data.startswith("menu:back"))
async def back_callback(callback: CallbackQuery) -> None:
    # Адрес родительского экрана передаётся в самом callback: menu:back:{target}
    data = callback.data or ""
    target = data.removeprefix("menu:back:") if data.startswith("menu:back:") else ""
    if not await open_screen(callback, target or "menu:main"):
        await render_main(callback)


@router.callback_query(F.data == "menu:cancel")
async def cancel_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await render_main(callback)
    await callback.answer("Действие отменено")


@router.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext) -> None:
    had_state = await state.get_state() is not None
    await state.clear()
    text = "Действие отменено." if had_state else "Незавершённых действий нет."
    kb = await build_main_menu(message.from_user.id)
    await message.answer(f"{text}\n\n{MAIN_TEXT}", reply_markup=kb)


@stale_router.callback_query(F.data)
async def stale_callback(callback: CallbackQuery) -> None:
    await callback.answer(STALE_TEXT, show_alert=True)
