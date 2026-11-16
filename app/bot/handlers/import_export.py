"""FSM импорта (Phase 08): файл → предпросмотр → сопоставление → прогресс → отчёт."""

import base64
import html
import re
from contextlib import suppress
from uuid import UUID

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.bot.keyboards.layout import smart_layout
from app.database.models import PermissionCode
from app.database.session import async_session
from app.services.auth.permissions import get_user_by_telegram_id, is_user_active
from app.services.categories import list_categories
from app.services.import_export.preview import (
    ImportPreview,
    PreviewError,
    RowError,
    preview_import,
)
from app.services.import_export.service import (
    ImportJobError,
    ImportStatus,
    cancel_import,
    create_import_job,
    get_import_status,
    import_records,
)
from app.services.permissions import PermissionDenied, PermissionService
from app.services.permissions.service import DENY_MESSAGES

router = Router()

MAX_IMPORT_BYTES = 10 * 1024 * 1024
MAX_CATEGORY_BUTTONS = 10
MAX_MAPPING_TEXT_LINES = 60
PREVIEW_TABLE_COLUMNS = 4
PREVIEW_VALUE_LENGTH = 24
CALLBACK_LIMIT = 64

_CATEGORY_CODE_RE = re.compile(r"^[a-z0-9_]{1,32}$")

IMPORT_BACK = "import:back"
IMPORT_CANCEL = "import:cancel"
IMPORT_RUN = "import:run"


class ImportFlow(StatesGroup):
    """Шаги мастера импорта."""

    waiting_category = State()
    waiting_file = State()
    preview = State()
    waiting_mapping = State()
    confirm = State()
    processing = State()


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------


def _keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    for row in rows:
        for button in row:
            if len(button.callback_data or "") > CALLBACK_LIMIT:
                raise ValueError("callback_data превышает лимит Telegram")
    return InlineKeyboardMarkup(inline_keyboard=smart_layout(rows))


def _category_keyboard(categories: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=title, callback_data=f"import:cat:{code}")]
        for code, title in categories[:MAX_CATEGORY_BUTTONS]
    ]
    rows.append([InlineKeyboardButton(text="✖️ Отменить", callback_data=IMPORT_CANCEL)])
    return _keyboard(rows)


def _file_keyboard() -> InlineKeyboardMarkup:
    return _keyboard(
        [
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data=IMPORT_BACK),
                InlineKeyboardButton(text="✖️ Отменить", callback_data=IMPORT_CANCEL),
            ]
        ]
    )


def _preview_keyboard() -> InlineKeyboardMarkup:
    return _keyboard(
        [
            [
                InlineKeyboardButton(text="✅ Продолжить", callback_data="import:mapping"),
                InlineKeyboardButton(text="✖️ Отменить", callback_data=IMPORT_CANCEL),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=IMPORT_BACK)],
        ]
    )


def _mapping_keyboard() -> InlineKeyboardMarkup:
    return _keyboard(
        [
            [
                InlineKeyboardButton(text="✅ Продолжить", callback_data="import:map:continue"),
                InlineKeyboardButton(text="⏭ Пропустить", callback_data="import:map:skip"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="import:back:preview"),
                InlineKeyboardButton(text="✖️ Отменить", callback_data=IMPORT_CANCEL),
            ],
        ]
    )


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return _keyboard(
        [
            [InlineKeyboardButton(text="✅ Продолжить", callback_data=IMPORT_RUN)],
            [
                InlineKeyboardButton(text="⬅️ Назад", callback_data="import:back:mapping"),
                InlineKeyboardButton(text="✖️ Отменить", callback_data=IMPORT_CANCEL),
            ],
        ]
    )


def _report_keyboard() -> InlineKeyboardMarkup:
    return _keyboard(
        [
            [InlineKeyboardButton(text="📥 Ещё импорт", callback_data="import:start")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="menu:main")],
        ]
    )


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


async def _load_user(session, telegram_id: int):
    user = await get_user_by_telegram_id(session, telegram_id)
    if not await is_user_active(user):
        return None
    if not await PermissionService.can(user, PermissionCode.IMPORT, "system", None, session):
        return None
    return user


async def _deny(callback: CallbackQuery) -> None:
    await callback.answer("Недостаточно прав для импорта", show_alert=True)


async def _edit(callback: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup) -> None:
    if callback.message is None:
        await callback.answer()
        return
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception:  # noqa: BLE001 - сообщение могло устареть
        await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


def _format_mapping(mapping: dict[str, str]) -> str:
    lines = [
        f"• <code>{html.escape(column)}</code> → <code>{html.escape(target)}</code>"
        for column, target in mapping.items()
    ]
    return "\n".join(lines) or "—"


def _sample_table(preview: ImportPreview) -> str:
    headers = preview.headers[:PREVIEW_TABLE_COLUMNS]
    if not headers or not preview.sample_rows:
        return "—"
    lines = [" | ".join(header[:PREVIEW_VALUE_LENGTH] for header in headers)]
    for row in preview.sample_rows[:3]:
        cells = []
        for header in headers:
            value = str(row.get(header, "")).replace("\n", " ")
            cells.append(value[:PREVIEW_VALUE_LENGTH])
        lines.append(" | ".join(cells))
    return "\n".join(lines)


def _preview_text(preview: ImportPreview, category_code: str) -> str:
    lines = [
        "📥 <b>Предпросмотр импорта</b>",
        f"Категория: <code>{html.escape(category_code)}</code>",
        f"Формат: {html.escape(preview.source_format)}"
        + (f" · кодировка: {html.escape(preview.encoding)}" if preview.encoding else ""),
        f"Тип записи: {html.escape(preview.record_type)}",
        "",
        (
            f"Строк: <b>{preview.total_rows}</b> · "
            f"валидных: <b>{preview.valid_rows}</b> · "
            f"с ошибками: <b>{preview.invalid_rows}</b>"
        ),
    ]
    if preview.duplicate_rows:
        lines.append(f"Дубликатов: {preview.duplicate_rows}")
    lines.extend(["", "Сопоставление полей:", _format_mapping(preview.suggested_mapping)])
    unmapped = [header for header in preview.headers if header not in preview.suggested_mapping]
    if unmapped:
        lines.append(
            "Не сопоставлены: " + ", ".join(html.escape(header) for header in unmapped[:8])
        )
    if preview.errors:
        lines.append("")
        lines.append("Ошибки (первые):")
        for error in preview.errors[:5]:
            field = f" ({html.escape(error.field)})" if error.field else ""
            lines.append(f"• строка {error.row}{field}: {html.escape(error.message)}")
        if preview.errors_truncated:
            lines.append("…")
    lines.extend(["", "Пример данных:", f"<pre>{html.escape(_sample_table(preview))}</pre>"])
    return "\n".join(lines)


def _mapping_text(mapping: dict[str, str]) -> str:
    lines = [
        "🔗 <b>Сопоставление полей</b>",
        "",
        (
            "Отправьте список вида <code>колонка = поле</code> (по одной на строку) "
            "или нажмите «Продолжить», чтобы оставить предложенное."
        ),
        "",
        _format_mapping(mapping),
    ]
    return "\n".join(lines)


def _confirm_text(preview: ImportPreview, category_code: str, mapping: dict[str, str]) -> str:
    return "\n".join(
        [
            "✅ <b>Подтверждение импорта</b>",
            f"Категория: <code>{html.escape(category_code)}</code>",
            f"Формат: {html.escape(preview.source_format)}",
            f"Будет обработано строк: <b>{preview.total_rows}</b>",
            f"Валидных: <b>{preview.valid_rows}</b> · с ошибками: <b>{preview.invalid_rows}</b>",
            "",
            "Итоговое сопоставление:",
            _format_mapping(mapping),
        ]
    )


def _progress_text(done: int, total: int) -> str:
    percent = 100 if total <= 0 else min(100, int(done * 100 / total))
    return f"⏳ Импорт: {done}/{total} ({percent}%)"


def _report_text(result, category_code: str) -> str:
    lines = [
        "📊 <b>Импорт завершён</b>" if not result.cancelled else "⛔ <b>Импорт отменён</b>",
        f"Категория: <code>{html.escape(category_code)}</code>",
        f"Всего строк: <b>{result.total_rows}</b>",
        f"Создано записей: <b>{result.success_rows}</b>",
        f"Ошибок: <b>{result.failed_rows}</b>",
    ]
    if result.skipped_rows:
        lines.append(f"Пропущено дубликатов: <b>{result.skipped_rows}</b>")
    if result.errors:
        lines.extend(["", "Ошибки (первые):"])
        for error in result.errors[:5]:
            lines.append(f"• строка {error.row}: {html.escape(error.message)}")
    return "\n".join(lines)


def _parse_mapping_message(text: str, headers: list[str]) -> dict[str, str]:
    known = {header.strip().casefold(): header for header in headers}
    mapping: dict[str, str] = {}
    for raw_line in text.splitlines()[:MAX_MAPPING_TEXT_LINES]:
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        source, target = line.split("=", 1)
        header = known.get(source.strip().casefold())
        field = target.strip().lower().replace(" ", "_")
        if header and field:
            mapping[header] = field
    return mapping


def _preview_payload(preview: ImportPreview) -> dict:
    """Только JSON-совместимые поля: FSM-хранилище не умеет dataclass."""
    return {
        "total_rows": preview.total_rows,
        "valid_rows": preview.valid_rows,
        "invalid_rows": preview.invalid_rows,
        "duplicate_rows": preview.duplicate_rows,
        "source_format": preview.source_format,
        "encoding": preview.encoding,
        "record_type": preview.record_type,
        "errors_truncated": preview.errors_truncated,
        "errors": [
            {"row": error.row, "field": error.field, "message": error.message}
            for error in preview.errors
        ],
        "headers": list(preview.headers),
        "mapping": dict(preview.suggested_mapping),
    }


def _preview_from_state(data: dict) -> ImportPreview | None:
    payload = data.get("preview")
    if not isinstance(payload, dict):
        return None
    return ImportPreview(
        total_rows=payload.get("total_rows", 0),
        valid_rows=payload.get("valid_rows", 0),
        invalid_rows=payload.get("invalid_rows", 0),
        duplicate_rows=payload.get("duplicate_rows", 0),
        source_format=payload.get("source_format", ""),
        encoding=payload.get("encoding"),
        record_type=payload.get("record_type", "OTHER"),
        errors_truncated=payload.get("errors_truncated", False),
        errors=[RowError(**error) for error in payload.get("errors", [])],
        headers=list(payload.get("headers", [])),
        suggested_mapping=dict(payload.get("mapping", {})),
    )


async def _render_categories(callback: CallbackQuery, state: FSMContext) -> None:
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if user is None:
            await _deny(callback)
            return
        categories = [
            (category.code, f"{category.name} ({category.code})")
            for category in await list_categories(session)
            if category.status == "ACTIVE"
        ]
    await state.update_data(category_code=None)
    await state.set_state(ImportFlow.waiting_category)
    await _edit(
        callback, "📥 <b>Импорт записей</b>\n\nВыберите категорию:", _category_keyboard(categories)
    )


async def _show_preview(
    callback: CallbackQuery, state: FSMContext, mapping: dict[str, str] | None = None
) -> None:
    data = await state.get_data()
    file_b64 = data.get("file_b64")
    if not file_b64:
        await _edit(callback, "Сначала отправьте файл для импорта.", _file_keyboard())
        await state.set_state(ImportFlow.waiting_file)
        return
    file_bytes = base64.b64decode(file_b64)
    filename = data.get("filename") or "import.dat"
    category_code = data.get("category_code")
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if user is None:
            await _deny(callback)
            return
        try:
            preview = await preview_import(
                session,
                file_bytes,
                filename,
                category_code or "",
                mapping=mapping,
            )
        except PreviewError as exc:
            await _edit(callback, f"⛔ {html.escape(str(exc))}", _file_keyboard())
            await state.set_state(ImportFlow.waiting_file)
            return
    await state.update_data(
        mapping=preview.suggested_mapping,
        headers=preview.headers,
        preview=_preview_payload(preview),
        file_size=len(file_bytes),
    )
    await state.set_state(ImportFlow.preview)
    await _edit(callback, _preview_text(preview, category_code or ""), _preview_keyboard())


async def _process_file(
    message: Message, state: FSMContext, file_bytes: bytes, filename: str
) -> None:
    data = await state.get_data()
    category_code = data.get("category_code")
    async with async_session() as session:
        user = await _load_user(session, message.from_user.id)
        if user is None:
            await message.answer("Недостаточно прав для импорта.")
            await state.clear()
            return
        try:
            preview = await preview_import(session, file_bytes, filename, category_code or "")
        except PreviewError as exc:
            await message.answer(f"⛔ {exc}", reply_markup=_file_keyboard())
            return
    await state.update_data(
        file_b64=base64.b64encode(file_bytes).decode(),
        filename=filename,
        mapping=preview.suggested_mapping,
        headers=preview.headers,
        preview=_preview_payload(preview),
    )
    await state.set_state(ImportFlow.preview)
    await message.answer(
        _preview_text(preview, category_code or ""),
        parse_mode="HTML",
        reply_markup=_preview_keyboard(),
    )


# ---------------------------------------------------------------------------
# Шаги мастера
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "import:start")
async def start_import(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _render_categories(callback, state)


@router.callback_query(F.data.startswith("import:cat:"))
async def choose_category(callback: CallbackQuery, state: FSMContext) -> None:
    code = (callback.data or "").removeprefix("import:cat:").strip()
    if not _CATEGORY_CODE_RE.match(code):
        await callback.answer("Неизвестная категория", show_alert=True)
        return
    async with async_session() as session:
        user = await _load_user(session, callback.from_user.id)
        if user is None:
            await _deny(callback)
            return
        from app.services.records.service import get_category_by_code

        category = await get_category_by_code(session, code)
        if category is None or category.status != "ACTIVE":
            await callback.answer("Категория недоступна", show_alert=True)
            return
        category_name = category.name
    data = await state.get_data()
    await state.update_data(category_code=code, category_name=category_name)
    if data.get("file_b64"):
        await _show_preview(callback, state)
        return
    await state.set_state(ImportFlow.waiting_file)
    await _edit(
        callback,
        f"📥 Импорт в категорию <b>{html.escape(category_name)}</b>\n\n"
        "Отправьте файл: XLSX, XLS, CSV, TSV, JSON, JSONL, XML или TXT "
        f"(до {MAX_IMPORT_BYTES // (1024 * 1024)} МБ).",
        _file_keyboard(),
    )


@router.callback_query(F.data == "import:mapping")
async def show_mapping(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    mapping = data.get("mapping") or {}
    await state.set_state(ImportFlow.waiting_mapping)
    await _edit(callback, _mapping_text(mapping), _mapping_keyboard())


@router.message(ImportFlow.waiting_file, F.document)
async def receive_file(message: Message, state: FSMContext) -> None:
    document = message.document
    if document is None:
        return
    if document.file_size and document.file_size > MAX_IMPORT_BYTES:
        await message.answer(
            f"Файл больше {MAX_IMPORT_BYTES // (1024 * 1024)} МБ. "
            "Разделите его на части и повторите импорт.",
            reply_markup=_file_keyboard(),
        )
        return
    buffer = await message.bot.download(document)
    if buffer is None:
        await message.answer("Не удалось скачать файл.", reply_markup=_file_keyboard())
        return
    file_bytes = buffer.read()
    await _process_file(message, state, file_bytes, document.file_name or "import.dat")


@router.message(ImportFlow.waiting_file)
async def receive_file_hint(message: Message) -> None:
    await message.answer(
        "Отправьте файл документом (XLSX, XLS, CSV, TSV, JSON, JSONL, XML или TXT).",
        reply_markup=_file_keyboard(),
    )


@router.callback_query(F.data == "import:back:preview")
async def back_to_preview(callback: CallbackQuery, state: FSMContext) -> None:
    await _show_preview(callback, state)


@router.callback_query(F.data == IMPORT_BACK)
async def back_to_categories(callback: CallbackQuery, state: FSMContext) -> None:
    await _render_categories(callback, state)


@router.message(ImportFlow.waiting_mapping, F.text)
async def edit_mapping(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    headers = data.get("headers") or []
    mapping = _parse_mapping_message(message.text or "", headers)
    if not mapping:
        await message.answer(
            "Не удалось разобрать сопоставление. Пример:\n"
            "<code>Название = title\nЛогин = login</code>",
            parse_mode="HTML",
        )
        return
    file_b64 = data.get("file_b64")
    if not file_b64:
        await message.answer("Файл потерян, начните импорт заново.")
        await state.clear()
        return
    category_code = data.get("category_code")
    async with async_session() as session:
        user = await _load_user(session, message.from_user.id)
        if user is None:
            await message.answer("Недостаточно прав для импорта.")
            await state.clear()
            return
        try:
            preview = await preview_import(
                session,
                base64.b64decode(file_b64),
                data.get("filename") or "import.dat",
                category_code or "",
                mapping=mapping,
            )
        except PreviewError as exc:
            await message.answer(f"⛔ {exc}")
            return
    await state.update_data(mapping=mapping, preview=_preview_payload(preview))
    await state.set_state(ImportFlow.waiting_mapping)
    await message.answer(
        _mapping_text(mapping), parse_mode="HTML", reply_markup=_mapping_keyboard()
    )


@router.callback_query(F.data == "import:map:skip")
@router.callback_query(F.data == "import:map:continue")
async def mapping_done(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    preview = _preview_from_state(data)
    if preview is None:
        await _show_preview(callback, state)
        return
    await state.set_state(ImportFlow.confirm)
    await _edit(
        callback,
        _confirm_text(preview, data.get("category_code") or "", data.get("mapping") or {}),
        _confirm_keyboard(),
    )


@router.callback_query(F.data == "import:back:mapping")
async def back_to_mapping(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(ImportFlow.waiting_mapping)
    await _edit(callback, _mapping_text(data.get("mapping") or {}), _mapping_keyboard())


@router.callback_query(F.data == IMPORT_RUN)
async def run_import(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    file_b64 = data.get("file_b64")
    if not file_b64:
        await _edit(callback, "Файл потерян. Начните импорт заново.", _report_keyboard())
        await state.clear()
        return
    await state.set_state(ImportFlow.processing)
    if callback.message is None:
        await callback.answer()
        return
    progress_message = await callback.message.answer(_progress_text(0, 0))
    await callback.answer()

    file_bytes = base64.b64decode(file_b64)
    filename = data.get("filename") or "import.dat"
    category_code = data.get("category_code")
    mapping = data.get("mapping") or {}
    preview = _preview_from_state(data)
    last_percent = {"value": -1}

    async def on_progress(done: int, total: int) -> None:
        percent = 100 if total <= 0 else int(done * 100 / total)
        if percent - last_percent["value"] < 10 and done != total:
            return
        last_percent["value"] = percent
        with suppress(Exception):
            await progress_message.edit_text(_progress_text(done, total))

    job_id: UUID | None = None
    try:
        async with async_session() as session:
            user = await _load_user(session, callback.from_user.id)
            if user is None:
                await progress_message.edit_text("Недостаточно прав для импорта.")
                await state.clear()
                return
            job = await create_import_job(
                session,
                user,
                category_code or "",
                None,
                source_format=getattr(preview, "source_format", None),
                total_rows=getattr(preview, "total_rows", 0),
            )
            job_id = job.id
            await state.update_data(job_id=str(job_id))
            result = await import_records(
                session,
                job_id,
                mapping,
                file_bytes=file_bytes,
                filename=filename,
                progress=on_progress,
            )
    except (ImportJobError, PermissionDenied) as exc:
        message = str(exc) or DENY_MESSAGES.get(getattr(exc, "reason", None), "Ошибка импорта.")
        await progress_message.edit_text(f"⛔ {html.escape(message)}")
        await state.clear()
        return
    except Exception as exc:  # noqa: BLE001 - показываем пользователю причину
        await progress_message.edit_text(f"⛔ Ошибка импорта: {html.escape(str(exc))}")
        await state.clear()
        return

    await progress_message.edit_text(_report_text(result, category_code or ""), parse_mode="HTML")
    await callback.message.answer(
        "Импорт завершён. Что дальше?",
        reply_markup=_report_keyboard(),
    )
    await state.clear()


@router.callback_query(F.data == IMPORT_CANCEL)
async def cancel_import_flow(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    job_id = data.get("job_id")
    cancelled = False
    if job_id:
        async with async_session() as session:
            user = await _load_user(session, callback.from_user.id)
            if user is not None:
                try:
                    cancelled = await cancel_import(session, job_id)
                except ImportJobError:
                    cancelled = False
    await state.clear()
    text = (
        "⛔ Импорт отменён."
        if not job_id
        else ("⛔ Импорт отменён." if cancelled else "Задача уже завершена, отмена не требуется.")
    )
    await _edit(callback, text, _report_keyboard())


@router.callback_query(F.data == "import:status")
async def show_status(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    job_id = data.get("job_id")
    if not job_id:
        await callback.answer("Активной задачи импорта нет", show_alert=True)
        return
    async with async_session() as session:
        status: ImportStatus | None = await get_import_status(session, job_id)
    if status is None:
        await callback.answer("Задача не найдена", show_alert=True)
        return
    await _edit(
        callback,
        f"📊 Задача {status.job_id}: {status.status.value}\n"
        f"Обработано {status.processed_rows} из {status.total_rows} ({status.percent}%)",
        _report_keyboard(),
    )
