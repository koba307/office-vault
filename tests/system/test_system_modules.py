import asyncio

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey

from app.bot.system.navigation import NavigationManager
from app.bot.system.confirmation import ConfirmationManager
from app.bot.system.process import ProcessManager, OperationStatus
from app.bot.system.progress import render_progress_bar
from app.bot.system.message_lifecycle import MessageLifecycleManager
from app.bot.system.keyboard_builder import KeyboardBuilder
from aiogram.types import InlineKeyboardButton


async def _state() -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=1, user_id=1)
    return FSMContext(storage, key)


def test_navigation_push_back_main():
    async def run():
        state = await _state()
        await NavigationManager.push_menu(state, "menu:a")
        await NavigationManager.push_menu(state, "menu:b")
        assert await NavigationManager.get_current_menu(state) == "menu:b"
        prev = await NavigationManager.go_back(state)
        assert prev == "menu:a"
        await NavigationManager.go_to_main_menu(state)
        assert await NavigationManager.get_current_menu(state) == "main"
    asyncio.run(run())


def test_confirmation_ownership_expiry():
    async def run():
        cm = ConfirmationManager()
        async def cb():
            return "done"
        item = cm.request(user_id=1, chat_id=1, action="delete", description="x", callback=cb)
        assert cm.get(item.token, 2) is None
        res = await cm.confirm(item.token, 1)
        assert res == "done"
        assert cm.get(item.token, 1) is None
    asyncio.run(run())


def test_process_cancel():
    async def run():
        pm = ProcessManager()
        ctx = pm.register(1, 1, "import")
        assert pm.request_cancel(str(ctx.operation_id), 1) is True
        assert pm.is_cancelled(ctx) is True
        pm.complete(ctx)
        assert not pm.request_cancel(str(ctx.operation_id), 1)
    asyncio.run(run())


def test_progress_bar():
    assert render_progress_bar(0) == "░" * 20 + " 0%"
    assert render_progress_bar(100).startswith("━" * 20)


def test_lifecycle_safe_delete():
    async def run():
        # no real bot; just policy registration
        ml = MessageLifecycleManager()
        ml.register(10, "DELETE_ON_CALLBACK")
        assert ml._policies[10] == "DELETE_ON_CALLBACK"
    asyncio.run(run())


def test_keyboard_builder_nav():
    rows = [[InlineKeyboardButton(text="X", callback_data="x")]]
    kb = KeyboardBuilder.build(rows, menu_id="sub")
    assert len(kb.inline_keyboard) == 2
    main_kb = KeyboardBuilder.build(rows, menu_id="main")
    assert len(main_kb.inline_keyboard) == 1
