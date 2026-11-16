from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def _is_main_menu(menu_id: str | None) -> bool:
    return menu_id in (None, "main", "menu:main")


class KeyboardBuilder:
    @staticmethod
    def build(rows: list[list[InlineKeyboardButton]], *, menu_id: str | None = None, show_nav: bool = True) -> InlineKeyboardMarkup:
        if show_nav and not _is_main_menu(menu_id):
            nav_row = [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="sys:nav:back"),
                InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main"),
            ]
            rows = [r for r in rows] + [nav_row]
        return InlineKeyboardMarkup(inline_keyboard=rows)
