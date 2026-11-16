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


def smart_layout(rows: list[list] | list) -> list[list]:
    """Автораскладка списка кнопок/рядов в несколько колонок.

    Принимает как список рядов (list[list[button]]), так и плоский список
    кнопок. Кнопки с короткими подписями группируются в ряды, длинные
    подписи получают отдельный ряд, чтобы текст был виден полностью.
    Навигационные кнопки («Назад», «Главное меню», «Отмена») всегда
    остаются на отдельных рядах.
    """
    flat: list = []
    for item in rows:
        if isinstance(item, list):
            flat.extend(item)
        else:
            flat.append(item)

    nav_markers = ("назад", "главное меню", "отмена", "отменить")
    result: list[list] = []
    current: list = []
    current_width = 0
    for btn in flat:
        text = getattr(btn, "text", str(btn))
        low = (text or "").lower()
        if any(marker in low for marker in nav_markers):
            if current:
                result.append(current)
                current = []
                current_width = 0
            result.append([btn])
            continue
        width = len(text or "") + 2
        # Длинные подписи всегда на отдельной строке.
        if len(text or "") > 24:
            if current:
                result.append(current)
                current = []
                current_width = 0
            result.append([btn])
            continue
        if current and current_width + width > 44:
            result.append(current)
            current = []
            current_width = 0
        current.append(btn)
        current_width += width
    if current:
        result.append(current)
    return result
