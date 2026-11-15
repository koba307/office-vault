from unittest.mock import AsyncMock

import pytest

from app.bot.handlers import menu, navigation
from app.bot.keyboards.pagination import (
    cancel_keyboard,
    confirm_keyboard,
    pagination_keyboard,
    total_pages,
)
from app.database.models import RecordType

TELEGRAM_CALLBACK_LIMIT = 64


def _all_callbacks(markup) -> list[str]:
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


def test_total_pages_bounds():
    assert total_pages(0, 10) == 1
    assert total_pages(10, 10) == 1
    assert total_pages(11, 10) == 2
    assert total_pages(25, 10) == 3


def test_pagination_hidden_for_single_page():
    assert pagination_keyboard(0, 0, 10, "p").inline_keyboard == []
    assert pagination_keyboard(10, 0, 10, "p").inline_keyboard == []


def test_pagination_first_page_has_no_prev():
    kb = pagination_keyboard(25, 0, 10, "menu:list:x:ALL:d")
    callbacks = _all_callbacks(kb)
    assert "menu:list:x:ALL:d:0" in callbacks
    assert "menu:list:x:ALL:d:1" in callbacks
    assert "menu:list:x:ALL:d:2" in callbacks
    assert callbacks[-1] == "menu:list:x:ALL:d:1"
    assert kb.inline_keyboard[0][-1].text == "➡️"


def test_pagination_middle_page_has_prev_and_next():
    kb = pagination_keyboard(100, 5, 10, "p")
    texts = [btn.text for btn in kb.inline_keyboard[0]]
    callbacks = _all_callbacks(kb)
    assert texts[0] == "⬅️"
    assert texts[-1] == "➡️"
    assert "p:4" in callbacks
    assert "p:6" in callbacks
    assert "· 6 ·" in texts


def test_pagination_last_page_has_no_next():
    kb = pagination_keyboard(25, 2, 10, "p")
    texts = [btn.text for btn in kb.inline_keyboard[0]]
    assert texts[0] == "⬅️"
    assert "➡️" not in texts


def test_pagination_clamps_out_of_range_page():
    kb = pagination_keyboard(25, 99, 10, "p")
    assert "· 3 ·" in [btn.text for btn in kb.inline_keyboard[0]]


def test_confirm_keyboard_callbacks():
    kb = confirm_keyboard("record:delete:1", "record:view:1")
    assert _all_callbacks(kb) == ["record:delete:1", "record:view:1"]


def test_cancel_keyboard_callback():
    kb = cancel_keyboard("menu:cancel")
    assert _all_callbacks(kb) == ["menu:cancel"]


def test_list_callback_parsing_valid():
    parsed = menu.parse_list_callback("menu:list:second_stage:INSTRUCTIONS:n:3")
    assert parsed == ("second_stage", "INSTRUCTIONS", "n", 3)


def test_list_callback_parsing_all_types():
    assert menu.parse_list_callback("menu:list:holodka:ALL:d:0") == ("holodka", "ALL", "d", 0)


@pytest.mark.parametrize(
    "data",
    [
        "menu:list:holodka:UNKNOWN:d:0",
        "menu:list:holodka:LINKS:x:0",
        "menu:list:holodka:LINKS:d:-1",
        "menu:list:holodka:LINKS:d:abc",
        "menu:list:holodka:LINKS:d",
        "menu:list::LINKS:d:0",
    ],
)
def test_list_callback_parsing_rejects_stale(data):
    assert menu.parse_list_callback(data) is None


def test_every_record_type_is_a_valid_filter():
    for t in RecordType:
        assert menu.parse_list_callback(f"menu:list:general:{t.value}:d:0") is not None
        assert t.value in menu.TYPE_TITLES


def test_category_type_buttons_fit_telegram_limit():
    for category in ("holodka", "second_stage", "closers", "general"):
        for t in menu.TYPE_TITLES:
            data = f"menu:list:{category}:{t}:{menu.SORT_DATE}:0"
            assert len(data.encode()) <= TELEGRAM_CALLBACK_LIMIT
            # Назад из списка, вложенный адрес родителя
            back = f"menu:back:menu:category:{category}"
            assert len(back.encode()) <= TELEGRAM_CALLBACK_LIMIT


def test_worst_case_callbacks_fit_telegram_limit():
    category = "second_stage"
    rid = "00000000-0000-0000-0000-000000000000"
    samples = [
        f"menu:list:{category}:INSTRUCTIONS:n:99",
        f"menu:back:menu:list:{category}:INSTRUCTIONS:n:99",
        f"record:view:{rid}",
        f"menu:ask:delete:{rid}",
        f"record:delete:{rid}",
        f"record:view:{rid}",
    ]
    for data in samples:
        assert len(data.encode()) <= TELEGRAM_CALLBACK_LIMIT, data


def test_breadcrumbs_join_path():
    text = menu.breadcrumbs("❄️ ХОЛОДКА", "📚 Инструкции")
    assert text == "🏠 Главная › ❄️ ХОЛОДКА › 📚 Инструкции"


async def test_back_uses_target_from_callback(monkeypatch):
    opened = []

    async def fake_open(callback, target):
        opened.append(target)
        return True

    monkeypatch.setattr(navigation, "open_screen", fake_open)

    callback = AsyncMock()
    callback.data = "menu:back:menu:category:holodka"
    await navigation.back_callback(callback)

    callback.data = "menu:back"
    await navigation.back_callback(callback)

    assert opened == ["menu:category:holodka", "menu:main"]


async def test_back_falls_back_to_main_for_unknown_target(monkeypatch):
    rendered = []

    async def fake_open(callback, target):
        return False

    async def fake_main(callback):
        rendered.append(True)

    monkeypatch.setattr(navigation, "open_screen", fake_open)
    monkeypatch.setattr(navigation, "render_main", fake_main)

    callback = AsyncMock()
    callback.data = "menu:back:menu:unknown"
    await navigation.back_callback(callback)

    assert rendered == [True]


async def test_stale_callback_is_answered_with_alert():
    callback = AsyncMock()
    callback.data = "menu:removed:42"
    await navigation.stale_callback(callback)

    callback.answer.assert_awaited_once()
    args, kwargs = callback.answer.await_args
    assert kwargs.get("show_alert") is True
    assert args[0] == menu.STALE_TEXT


async def test_cancel_callback_clears_fsm_and_renders_main(monkeypatch):
    state = AsyncMock()
    rendered = []

    async def fake_main(callback):
        rendered.append(True)

    monkeypatch.setattr(navigation, "render_main", fake_main)

    callback = AsyncMock()
    await navigation.cancel_callback(callback, state)

    state.clear.assert_awaited_once()
    assert rendered == [True]
    callback.answer.assert_awaited_once_with("Действие отменено")


async def test_cancel_command_reports_active_flow(monkeypatch):
    async def fake_menu(telegram_id):
        return None

    monkeypatch.setattr(navigation, "build_main_menu", fake_menu)

    state = AsyncMock()
    state.get_state.return_value = "AddRecord:waiting_title"
    message = AsyncMock()
    message.from_user.id = 1

    await navigation.cancel_command(message, state)

    state.clear.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert text.startswith("Действие отменено.")


async def test_cancel_command_without_flow(monkeypatch):
    async def fake_menu(telegram_id):
        return None

    monkeypatch.setattr(navigation, "build_main_menu", fake_menu)

    state = AsyncMock()
    state.get_state.return_value = None
    message = AsyncMock()
    message.from_user.id = 1

    await navigation.cancel_command(message, state)

    text = message.answer.await_args.args[0]
    assert text.startswith("Незавершённых действий нет.")
