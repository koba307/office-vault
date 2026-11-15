import math

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Сколько номеров страниц показывать вокруг текущей
PAGE_WINDOW = 2


def total_pages(total: int, page_size: int) -> int:
    return max(1, math.ceil(total / page_size))


def pagination_keyboard(total: int, page: int, page_size: int, prefix: str) -> InlineKeyboardMarkup:
    """Клавиатура страниц. Callback каждой кнопки: f"{prefix}:{номер страницы}" (с 0)."""
    pages = total_pages(total, page_size)
    if pages <= 1:
        return InlineKeyboardMarkup(inline_keyboard=[])

    page = min(max(page, 0), pages - 1)
    row: list[InlineKeyboardButton] = []

    if page > 0:
        row.append(InlineKeyboardButton(text="⬅️", callback_data=f"{prefix}:{page - 1}"))

    start = max(0, page - PAGE_WINDOW)
    end = min(pages, page + PAGE_WINDOW + 1)
    for idx in range(start, end):
        label = f"· {idx + 1} ·" if idx == page else str(idx + 1)
        row.append(InlineKeyboardButton(text=label, callback_data=f"{prefix}:{idx}"))

    if page < pages - 1:
        row.append(InlineKeyboardButton(text="➡️", callback_data=f"{prefix}:{page + 1}"))

    return InlineKeyboardMarkup(inline_keyboard=[row])


def confirm_keyboard(yes_callback: str, no_callback: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да", callback_data=yes_callback),
                InlineKeyboardButton(text="❌ Нет", callback_data=no_callback),
            ]
        ]
    )


def cancel_keyboard(cancel_callback: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data=cancel_callback)]]
    )
