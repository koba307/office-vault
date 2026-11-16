from __future__ import annotations

from aiogram.types import InlineKeyboardButton


def auto_layout(
    buttons: list[InlineKeyboardButton],
    *,
    max_row_width: int = 42,
) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    current: list[InlineKeyboardButton] = []
    current_width = 0
    for button in buttons:
        width = len(button.text or "") + 2
        if current and current_width + width > max_row_width:
            rows.append(current)
            current = []
            current_width = 0
        current.append(button)
        current_width += width
    if current:
        rows.append(current)
    return rows
