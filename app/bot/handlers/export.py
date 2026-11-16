"""FSM экспорта записей (Phase 08).

Шаги: категория → тип → записи → поля → формат → вложения → секреты → подтверждение →
прогресс → отчёт. Всё идёт в одном сообщении-панели, которое редактируется на каждом шаге.
Кнопки: «Пропустить» берёт значение по умолчанию, «Назад» возвращает на предыдущий шаг,
«Отменить» сбрасывает сценарий. Права проверяются сервисом на каждом шаге и при запуске.
"""

import html
import logging
from typing import Any
from uuid import UUID

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from app.bot.handlers.menu import TYPE_TITLES
from app.bot.keyboards.layout import auto_layout, smart_layout
from app.database.models import Category, PermissionCode, RecordType, User
from app.database.session import async_session
from app.services.auth.permissions import get_user_by_telegram_id, is_user_active
from app.services.import_export.export import (
    EXPORT_MAX_RECORDS,
    ExportError,
    available_export_columns,
    can_export_secrets,
    parse_record_ids_text,
)
from app.services.import_export.export_jobs import create_export_job, run_export_job
from app.services.permissions import PermissionService
from app.services.permissions.service import TARGET_CATEGORY

logger = logging.getLogger(__name__)

router = Router()

TELEGRAM_FILE_LIMIT = 50 * 1024 * 1024
STEPS = ("category", "type", "records", "fields", "format", "attachments", "secrets", "confirm")
FORMAT_TITLES = {
    "csv": "CSV",
    "xlsx": "XLSX (Excel)",
    "json": "JSON",
    "zip": "ZIP (файлы и манифест)",
}
DEFAULT_FORMAT = "csv"


class ExportRecords(StatesGroup):
    waiting_category = State()
    waiting_type = State()
    waiting_records = State()
    waiting_fields = State()
    waiting_format = State()
    waiting_attachments = State()
    waiting_secrets = State()
    waiting_confirm = State()


STATE_BY_STEP: dict[str, State] = {
    "category": ExportRecords.waiting_category,
    "type": ExportRecords.waiting_type,
    "records": ExportRecords.waiting_records,
    "fields": ExportRecords.waiting_fields,
    "format": ExportRecords.waiting_format,
    "attachments": ExportRecords.waiting_attachments,
    "secrets": ExportRecords.waiting_secrets,
    "confirm": ExportRecords.waiting_confirm,
}


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def _btn(text: str, callback: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback)


def _nav_rows(
    data: dict[str, Any],
    *,
    primary: bool = False,
    skip: bool = True,
) -> list[list[InlineKeyboardButton]]:
    """Нижние кнопки шага: «Продолжить»/«Пропустить» и служебный ряд «Назад»/«Отменить»."""
    first: list[InlineKeyboardButton] = []
    if primary:
        first.append(_btn("▶️ Продолжить", "exp:next"))
    if skip:
        first.append(_btn("⏭ Пропустить", "exp:skip"))
    rows = [first] if first else []
    service = []
    if data.get("history"):
        service.append(_btn("◀️ Назад", "exp:back"))
    service.append(_btn("✖️ Отменить", "exp:cancel"))
    rows.append(service)
    return rows


def _markup(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=smart_layout(rows))


def _two_columns(buttons: list[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    return [buttons[i : i + 2] for i in range(0, len(buttons), 2)]


def _can_secrets_from(data: dict[str, Any]) -> bool:
    return bool(data.get("can_secrets"))


def _step_order(data: dict[str, Any]) -> list[str]:
    """Шаги мастера. Шаг секретов виден только при праве EXPORT_SECRETS."""
    return [step for step in STEPS if step != "secrets" or _can_secrets_from(data)]


def _next_step(data: dict[str, Any]) -> str:
    order = _step_order(data)
    return order[order.index(data["step"]) + 1]


def _record_ids_of(data: dict[str, Any]) -> list[UUID] | None:
    raw = data.get("record_ids")
    return [UUID(item) for item in raw] if raw is not None else None


async def _active_user(session, telegram_id: int) -> User | None:
    user = await get_user_by_telegram_id(session, telegram_id)
    return user if await is_user_active(user) else None


async def _show(bot, chat_id: int, message_id: int, text: str, markup=None) -> None:
    """Обновляет панель. «Сообщение не изменилось» — не ошибка."""
    try:
        await bot.edit_message_text(
            text=text, chat_id=chat_id, message_id=message_id, reply_markup=markup
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


async def _render(
    step: str, data: dict[str, Any], session, user: User
) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и клавиатура шага. Все варианты берутся из БД с учётом прав пользователя."""
    order = _step_order(data)
    header = f"📤 <b>Экспорт записей</b> · шаг {order.index(step) + 1} из {len(order)}\n"

    if step == "category":
        categories = (
            (
                await session.execute(
                    select(Category)
                    .where(Category.status == "ACTIVE")
                    .order_by(Category.sort_order, Category.code)
                )
            )
            .scalars()
            .all()
        )
        buttons = []
        for category in categories:
            if await PermissionService.can(
                user, PermissionCode.EXPORT, TARGET_CATEGORY, category.id, session
            ):
                buttons.append(_btn(category.name, f"exp:cat:{category.code}"))
        rows = auto_layout(buttons) + _nav_rows(data)
        return header + "Выберите категорию или пропустите, чтобы взять все доступные.", _markup(
            rows
        )

    if step == "type":
        buttons = [_btn(TYPE_TITLES[rtype], f"exp:type:{rtype.value}") for rtype in RecordType]
        return header + "Выберите тип записей или пропустите — будут все типы.", _markup(
            auto_layout(buttons) + _nav_rows(data)
        )

    if step == "records":
        return (
            header
            + f"Отправьте ID записей (UUID) через пробел или запятую — не более {EXPORT_MAX_RECORDS}.\n"
            "Или пропустите, чтобы выгрузить все доступные записи выборки.",
            _markup(_nav_rows(data)),
        )

    if step == "fields":
        names = await available_export_columns(
            session,
            user,
            category_code=data.get("category_code"),
            record_type=data.get("record_type"),
            record_ids=_record_ids_of(data),
            with_attachments=False,
        )
        flags = _field_flags(data, names)
        data["field_names"] = names
        data["field_flags"] = flags
        buttons = [
            _btn(f"{'✅' if flag else '⬜'} {name}", f"exp:field:{index}")
            for index, (name, flag) in enumerate(zip(names, flags, strict=True))
        ]
        text = (
            header + "Отметьте поля для выгрузки и нажмите «Продолжить». "
            "«Пропустить» выгрузит все поля."
        )
        if not names:
            text += "\nВ выборке нет полей."
        return text, _markup(auto_layout(buttons) + _nav_rows(data, primary=True))

    if step == "format":
        buttons = [_btn(title, f"exp:fmt:{key}") for key, title in FORMAT_TITLES.items()]
        return header + "Выберите формат файла.", _markup(auto_layout(buttons) + _nav_rows(data))

    if step == "attachments":
        state = "включены" if data.get("include_files") else "выключены"
        text = (
            header + f"Вложения: <b>{state}</b>.\n"
            "В таблицах (CSV, XLSX, JSON) — имена файлов. В ZIP — сами файлы с манифестом.\n"
            "Скачиваются только файлы, на которые у вас есть право."
        )
        rows = [[_btn("📎 Вложения: вкл/выкл", "exp:files")]] + _nav_rows(data, primary=True)
        return text, _markup(rows)

    if step == "secrets":
        state = "включены" if data.get("include_secrets") else "выключены"
        text = (
            header + f"Секреты (пароли и секретные поля): <b>{state}</b>.\n"
            "⚠️ Включение выводит секреты в открытом виде. Действие попадёт в журнал аудита."
        )
        rows = [[_btn("🔐 Секреты: вкл/выкл", "exp:secrets")]] + _nav_rows(data, primary=True)
        return text, _markup(rows)

    return await _render_confirm(header, data, session, user)


async def _render_confirm(
    header: str, data: dict[str, Any], session, user: User
) -> tuple[str, InlineKeyboardMarkup]:
    category_name = data.get("category_name") or "все доступные"
    type_title = (
        TYPE_TITLES[RecordType(data["record_type"])] if data.get("record_type") else "все типы"
    )
    ids = data.get("record_ids")
    records = f"указано ID: {len(ids)}" if ids is not None else "все доступные"
    fields = data.get("fields")
    fields_text = "все" if fields is None else f"выбрано: {len(fields)}"
    text = (
        header + "Проверьте параметры:\n"
        f"Категория: {_esc(category_name)}\n"
        f"Тип: {type_title}\n"
        f"Записи: {records}\n"
        f"Поля: {fields_text}\n"
        f"Формат: {FORMAT_TITLES[data.get('export_format', DEFAULT_FORMAT)]}\n"
        f"Вложения: {'да' if data.get('include_files') else 'нет'}\n"
        f"Секреты: {'ДА, в открытом виде' if data.get('include_secrets') else 'нет (маскируются)'}"
    )
    if data.get("include_secrets"):
        text += "\n\n⚠️ Файл будет содержать пароли в открытом виде."
    rows = [[_btn("✅ Подтвердить экспорт", "exp:run")]]
    rows.append([_btn("◀️ Назад", "exp:back"), _btn("✖️ Отменить", "exp:cancel")])
    return text, _markup(rows)


def _field_flags(data: dict[str, Any], names: list[str]) -> list[bool]:
    """Отметки полей сохраняются, если список полей не изменился; иначе отмечаем все."""
    if data.get("field_names") == names and data.get("field_flags") is not None:
        return list(data["field_flags"])
    return [True] * len(names)


async def _enter(
    bot,
    chat_id: int,
    state: FSMContext,
    session,
    user: User,
    step: str,
    data: dict[str, Any],
    *,
    history: list[str],
) -> None:
    """Переходит на шаг: сохраняет состояние FSM и перерисовывает панель."""
    data["step"] = step
    data["history"] = history
    text, markup = await _render(step, data, session, user)
    await state.set_data(data)
    await state.set_state(STATE_BY_STEP[step])
    await _show(bot, chat_id, data["panel"], text, markup)


async def _advance(
    bot,
    chat_id: int,
    state: FSMContext,
    session,
    user: User,
    data: dict[str, Any],
) -> None:
    current = data["step"]
    await _enter(
        bot,
        chat_id,
        state,
        session,
        user,
        _next_step(data),
        data,
        history=[*data.get("history", []), current],
    )


@router.callback_query(F.data == "export:csv")
async def start_export(callback: CallbackQuery, state: FSMContext) -> None:
    """Точка входа из меню: проверка права EXPORT и создание панели мастера."""
    async with async_session() as session:
        user = await _active_user(session, callback.from_user.id)
        if user is None:
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not await PermissionService.can(user, PermissionCode.EXPORT, None, None, session):
            await callback.answer("Нет права на экспорт", show_alert=True)
            return
        can_secrets = await can_export_secrets(session, user)
        data: dict[str, Any] = {
            "panel": None,
            "history": [],
            "step": "category",
            "can_secrets": can_secrets,
            "category_code": None,
            "category_name": None,
            "record_type": None,
            "record_ids": None,
            "field_names": None,
            "field_flags": None,
            "fields": None,
            "export_format": DEFAULT_FORMAT,
            "include_files": False,
            "include_secrets": False,
        }
        await state.clear()
        panel = await callback.message.answer("Загружаю мастер экспорта…")
        data["panel"] = panel.message_id
        await _enter(
            callback.bot,
            panel.chat.id,
            state,
            session,
            user,
            "category",
            data,
            history=[],
        )
    await callback.answer()


@router.callback_query(F.data.startswith("exp:cat:"), StateFilter(ExportRecords.waiting_category))
async def pick_category(callback: CallbackQuery, state: FSMContext) -> None:
    code = (callback.data or "").split(":", 2)[2]
    async with async_session() as session:
        user = await _active_user(session, callback.from_user.id)
        if user is None:
            await callback.answer("Нет доступа", show_alert=True)
            return
        category = (
            await session.execute(select(Category).where(Category.code == code))
        ).scalar_one_or_none()
        if category is None or category.status != "ACTIVE":
            await callback.answer("Категория недоступна", show_alert=True)
            return
        if not await PermissionService.can(
            user, PermissionCode.EXPORT, TARGET_CATEGORY, category.id, session
        ):
            await callback.answer("Нет права экспорта в эту категорию", show_alert=True)
            return
        data = await state.get_data()
        data["category_code"] = category.code
        data["category_name"] = category.name
        await _advance(callback.bot, callback.message.chat.id, state, session, user, data)
    await callback.answer()


@router.callback_query(F.data.startswith("exp:type:"), StateFilter(ExportRecords.waiting_type))
async def pick_type(callback: CallbackQuery, state: FSMContext) -> None:
    raw = (callback.data or "").split(":", 2)[2]
    try:
        rtype = RecordType(raw)
    except ValueError:
        await callback.answer("Неизвестный тип", show_alert=True)
        return
    async with async_session() as session:
        user = await _active_user(session, callback.from_user.id)
        if user is None:
            await callback.answer("Нет доступа", show_alert=True)
            return
        data = await state.get_data()
        data["record_type"] = rtype.value
        await _advance(callback.bot, callback.message.chat.id, state, session, user, data)
    await callback.answer()


@router.message(StateFilter(ExportRecords.waiting_records), F.text)
async def receive_record_ids(message: Message, state: FSMContext) -> None:
    try:
        ids = parse_record_ids_text(message.text or "")
    except ExportError as exc:
        await message.answer(f"❌ {_esc(exc)}")
        return
    async with async_session() as session:
        user = await _active_user(session, message.from_user.id)
        if user is None:
            await state.clear()
            await message.answer("Нет доступа")
            return
        data = await state.get_data()
        data["record_ids"] = [str(item) for item in ids]
        await _advance(message.bot, message.chat.id, state, session, user, data)


@router.callback_query(F.data.startswith("exp:field:"), StateFilter(ExportRecords.waiting_fields))
async def toggle_field(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        index = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректное поле", show_alert=True)
        return
    async with async_session() as session:
        user = await _active_user(session, callback.from_user.id)
        if user is None:
            await callback.answer("Нет доступа", show_alert=True)
            return
        data = await state.get_data()
        flags = list(data.get("field_flags") or [])
        if not 0 <= index < len(flags):
            await callback.answer("Список полей обновился, откройте шаг снова", show_alert=True)
            return
        flags[index] = not flags[index]
        data["field_flags"] = flags
        await _enter(
            callback.bot,
            callback.message.chat.id,
            state,
            session,
            user,
            "fields",
            data,
            history=data.get("history", []),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("exp:fmt:"), StateFilter(ExportRecords.waiting_format))
async def pick_format(callback: CallbackQuery, state: FSMContext) -> None:
    fmt = (callback.data or "").split(":", 2)[2]
    if fmt not in FORMAT_TITLES:
        await callback.answer("Неизвестный формат", show_alert=True)
        return
    async with async_session() as session:
        user = await _active_user(session, callback.from_user.id)
        if user is None:
            await callback.answer("Нет доступа", show_alert=True)
            return
        data = await state.get_data()
        data["export_format"] = fmt
        await _advance(callback.bot, callback.message.chat.id, state, session, user, data)
    await callback.answer()


@router.callback_query(F.data == "exp:files", StateFilter(ExportRecords.waiting_attachments))
async def toggle_files(callback: CallbackQuery, state: FSMContext) -> None:
    async with async_session() as session:
        user = await _active_user(session, callback.from_user.id)
        if user is None:
            await callback.answer("Нет доступа", show_alert=True)
            return
        data = await state.get_data()
        data["include_files"] = not data.get("include_files")
        await _enter(
            callback.bot,
            callback.message.chat.id,
            state,
            session,
            user,
            "attachments",
            data,
            history=data.get("history", []),
        )
    await callback.answer()


@router.callback_query(F.data == "exp:secrets", StateFilter(ExportRecords.waiting_secrets))
async def toggle_secrets(callback: CallbackQuery, state: FSMContext) -> None:
    async with async_session() as session:
        user = await _active_user(session, callback.from_user.id)
        if user is None:
            await callback.answer("Нет доступа", show_alert=True)
            return
        data = await state.get_data()
        if not _can_secrets_from(data):
            await callback.answer("Экспорт секретов недоступен для вашей роли", show_alert=True)
            return
        data["include_secrets"] = not data.get("include_secrets")
        await _enter(
            callback.bot,
            callback.message.chat.id,
            state,
            session,
            user,
            "secrets",
            data,
            history=data.get("history", []),
        )
    await callback.answer()


@router.callback_query(F.data == "exp:next", StateFilter(ExportRecords))
async def next_step(callback: CallbackQuery, state: FSMContext) -> None:
    """«Продолжить» на шагах с отметками: фиксирует выбор и идёт дальше."""
    async with async_session() as session:
        user = await _active_user(session, callback.from_user.id)
        if user is None:
            await callback.answer("Нет доступа", show_alert=True)
            return
        data = await state.get_data()
        step = data.get("step")
        if step == "fields":
            names = data.get("field_names") or []
            flags = data.get("field_flags") or []
            selected = [name for name, flag in zip(names, flags, strict=False) if flag]
            if not selected:
                await callback.answer("Выберите хотя бы одно поле", show_alert=True)
                return
            data["fields"] = None if len(selected) == len(names) else selected
        elif step not in ("attachments", "secrets"):
            await callback.answer("Этот шаг нужно пройти кнопкой", show_alert=True)
            return
        await _advance(callback.bot, callback.message.chat.id, state, session, user, data)
    await callback.answer()


@router.callback_query(F.data == "exp:skip", StateFilter(ExportRecords))
async def skip_step(callback: CallbackQuery, state: FSMContext) -> None:
    """«Пропустить»: значение по умолчанию для текущего шага."""
    async with async_session() as session:
        user = await _active_user(session, callback.from_user.id)
        if user is None:
            await callback.answer("Нет доступа", show_alert=True)
            return
        data = await state.get_data()
        step = data.get("step")
        if step == "category":
            data["category_code"] = None
            data["category_name"] = None
        elif step == "type":
            data["record_type"] = None
        elif step == "records":
            data["record_ids"] = None
        elif step == "fields":
            data["fields"] = None
        elif step == "format":
            data["export_format"] = DEFAULT_FORMAT
        elif step == "attachments":
            data["include_files"] = False
        elif step == "secrets":
            data["include_secrets"] = False
        else:
            await callback.answer("На этом шаге пропуск недоступен", show_alert=True)
            return
        await _advance(callback.bot, callback.message.chat.id, state, session, user, data)
    await callback.answer()


@router.callback_query(F.data == "exp:back", StateFilter(ExportRecords))
async def back_step(callback: CallbackQuery, state: FSMContext) -> None:
    async with async_session() as session:
        user = await _active_user(session, callback.from_user.id)
        if user is None:
            await callback.answer("Нет доступа", show_alert=True)
            return
        data = await state.get_data()
        history = list(data.get("history", []))
        if not history:
            await callback.answer("Это первый шаг", show_alert=True)
            return
        previous = history.pop()
        await _enter(
            callback.bot,
            callback.message.chat.id,
            state,
            session,
            user,
            previous,
            data,
            history=history,
        )
    await callback.answer()


@router.callback_query(F.data == "exp:cancel", StateFilter(ExportRecords))
async def cancel_export(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Экспорт отменён.")
    await callback.answer()


@router.callback_query(F.data == "exp:run", StateFilter(ExportRecords.waiting_confirm))
async def run_export(callback: CallbackQuery, state: FSMContext) -> None:
    """Запуск задачи: прогресс в панели, файл в чат, отчёт в панели."""
    data = await state.get_data()
    await state.clear()
    chat_id = callback.message.chat.id
    panel_id = data["panel"]
    bot = callback.bot

    async def progress(percent: int, text: str) -> None:
        await _show(bot, chat_id, panel_id, f"⏳ <b>Экспорт</b>\n{percent}% · {_esc(text)}")

    await callback.answer()
    async with async_session() as session:
        user = await _active_user(session, callback.from_user.id)
        if user is None:
            await _show(bot, chat_id, panel_id, "❌ Нет доступа.")
            return
        try:
            job = await create_export_job(
                session,
                user,
                data.get("category_code"),
                data.get("export_format", DEFAULT_FORMAT),
                record_type=data.get("record_type"),
                record_ids=_record_ids_of(data),
                fields=data.get("fields"),
                include_files=bool(data.get("include_files")),
                include_secrets=bool(data.get("include_secrets")),
            )
            artifact = await run_export_job(session, job.id, user=user, progress=progress)
        except ExportError as exc:
            await _show(bot, chat_id, panel_id, f"❌ {_esc(exc)}")
            return
        except Exception:
            logger.exception("Экспорт завершился с внутренней ошибкой")
            await _show(
                bot, chat_id, panel_id, "❌ Не удалось сформировать экспорт. Попробуйте позже."
            )
            return

    report = [
        "✅ <b>Экспорт готов</b>",
        f"Формат: {FORMAT_TITLES.get(artifact.format, artifact.format)}",
        f"Записей: {artifact.record_count}",
    ]
    if artifact.format == "zip":
        report.append(f"Файлов в архиве: {artifact.file_count}")
        if artifact.skipped_files:
            report.append(
                f"Пропущено файлов: {len(artifact.skipped_files)} (причины в manifest.json)"
            )
    if artifact.include_secrets:
        report.append(
            "🔐 Файл содержит секреты в открытом виде. Храните его защищённо "
            "и удалите сообщение после использования."
        )
    if artifact.expires_at is not None:
        report.append(
            f"Копия в хранилище будет удалена после {artifact.expires_at:%d.%m.%Y %H:%M} UTC."
        )
    report.append(f"ID задачи: <code>{artifact.job_id}</code>")

    if len(artifact.data) > TELEGRAM_FILE_LIMIT:
        report.append("❌ Файл больше 50 МБ — Telegram его не примет. Сузьте выборку.")
        await _show(bot, chat_id, panel_id, "\n".join(report))
        return

    await callback.message.answer_document(
        BufferedInputFile(artifact.data, filename=artifact.filename),
        caption="🔐 Секреты в открытом виде" if artifact.include_secrets else "📤 Экспорт записей",
    )
    await _show(bot, chat_id, panel_id, "\n".join(report))


@router.callback_query(F.data.startswith("exp:"))
async def stale_export_callback(callback: CallbackQuery) -> None:
    """Кнопка от уже закрытой панели экспорта или от другого шага."""
    await callback.answer("Сессия экспорта истекла. Начните заново.", show_alert=True)
