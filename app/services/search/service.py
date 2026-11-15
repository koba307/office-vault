"""Permission-aware поиск по записям (ТЗ §11).

Поиск выполняется только по активным записям и возвращает лишь те записи,
которые пользователь вправе просматривать. Проверка прав — Default Deny:
любая ошибка или отсутствие доступа исключает запись из результата, поэтому
названия закрытых записей никогда не попадают в ответ.

Пароли и секреты не участвуют в поиске: поля типов PASSWORD/SECRET,
зашифрованные значения и служебные ключи секретов исключены из SQL-условий.
"""

from datetime import UTC, date, datetime, time
from typing import Any
from uuid import UUID

from sqlalchemy import String, Text, and_, case, cast, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from app.database.models import (
    Category,
    FieldType,
    File,
    PermissionCode,
    Record,
    RecordField,
    RecordFile,
    RecordTag,
    RecordType,
    Tag,
    User,
)
from app.services.permissions.context import load_access_context
from app.services.permissions.record import record_denial_reason

# Подписи типов для поиска по «типу информации» (без зависимости от bot-слоя).
RECORD_TYPE_LABELS: dict[RecordType, str] = {
    RecordType.CREDENTIALS: "логины и пароли",
    RecordType.SIP: "sip и телефония",
    RecordType.LINKS: "ссылки",
    RecordType.SETTINGS: "настройки",
    RecordType.INSTRUCTIONS: "инструкции",
    RecordType.FILES: "файлы и документы",
    RecordType.OTHER: "прочее",
}

SECRET_FIELD_TYPES = (FieldType.PASSWORD.value, FieldType.SECRET.value)

# Даже если тип поля испорчен, значения с такими ключами не индексируются.
SECRET_FIELD_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "api_key",
        "access_key",
        "private_key",
        "sip_password",
    }
)

MAX_QUERY_LENGTH = 256


def normalize_query(query: str | None) -> str:
    """Приводит запрос к нижнему регистру и схлопывает пробелы."""
    return " ".join(str(query or "").split()).lower()[:MAX_QUERY_LENGTH]


def tokenize_query(query: str | None) -> list[str]:
    """Слова запроса без дублей: каждое слово должно найтись (AND)."""
    tokens: list[str] = []
    for token in normalize_query(query).split():
        if token not in tokens:
            tokens.append(token)
    return tokens


def record_types_matching(token: str) -> tuple[RecordType, ...]:
    """Типы записей, в названии или коде которых встречается строка."""
    needle = normalize_query(token)
    if not needle:
        return ()
    matched: list[RecordType] = []
    for record_type in RecordType:
        haystack = f"{record_type.value.lower()} {RECORD_TYPE_LABELS.get(record_type, '')}"
        if needle in haystack:
            matched.append(record_type)
    return tuple(matched)


def record_types_for_query(query: str | None) -> tuple[RecordType, ...]:
    """Тип, если весь запрос целиком описывает тип информации."""
    tokens = tokenize_query(query)
    if not tokens:
        return ()
    matched: list[RecordType] = []
    for record_type in RecordType:
        haystack = f"{record_type.value.lower()} {RECORD_TYPE_LABELS.get(record_type, '')}"
        if all(token in haystack for token in tokens):
            matched.append(record_type)
    return tuple(matched)


def normalize_tags(tags: Any) -> list[str]:
    """Фильтр тегов принимает строку или последовательность названий."""
    if tags is None:
        return []
    if isinstance(tags, str):
        raw_items = [tags]
    else:
        try:
            raw_items = list(tags)
        except TypeError:
            raw_items = [tags]
    normalized: list[str] = []
    for item in raw_items:
        # Поддерживаем и готовые объекты Tag, и строковые названия.
        raw_name = getattr(item, "name", item)
        name = " ".join(str(raw_name or "").split()).lower()
        if name and name not in normalized:
            normalized.append(name)
    return normalized


def normalize_record_type(record_type: RecordType | str | None) -> RecordType | None:
    """None и ALL означают отсутствие фильтра; неизвестное значение не пропускает записи."""
    if record_type is None:
        return None
    if isinstance(record_type, RecordType):
        return record_type
    value = str(record_type).strip().upper()
    if value in {"", "ALL"}:
        return None
    try:
        return RecordType(value)
    except ValueError:
        return None


def _as_datetime(value: date | datetime | str | None, *, end: bool = False) -> datetime | None:
    """Приводит границу даты к timezone-aware datetime; мусор игнорируется."""
    if value is None:
        return None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime.combine(value, time.max if end else time.min)
    else:
        raw = str(value).strip()
        if not raw:
            return None
        moment = None
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S"):
            try:
                moment = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        if moment is None:
            try:
                moment = datetime.fromisoformat(raw)
            except ValueError:
                return None
        if ":" not in raw and "T" not in raw and " " not in raw:
            moment = datetime.combine(moment.date(), time.max if end else time.min)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def _secret_field_condition():
    """Условие «поле не является секретом и не зашифровано»."""
    return (
        RecordField.encrypted_value.is_(None),
        RecordField.field_type.notin_(SECRET_FIELD_TYPES),
        RecordField.field_key.notin_(SECRET_FIELD_KEYS),
    )


def _field_exists(match):
    """EXISTS по текстовым полям записи без секретов."""
    return exists(
        select(RecordField.id).where(
            RecordField.record_id == Record.id,
            *_secret_field_condition(),
            match,
        )
    )


def _tag_exists(match):
    return exists(
        select(RecordTag.record_id)
        .join(Tag, Tag.id == RecordTag.tag_id)
        .where(RecordTag.record_id == Record.id, match)
    )


def _file_exists(match):
    return exists(
        select(RecordFile.record_id)
        .join(File, File.id == RecordFile.file_id)
        .where(
            RecordFile.record_id == Record.id,
            File.deleted_at.is_(None),
            match,
        )
    )


async def _resolve_category_id(session: AsyncSession, category_code: str) -> UUID | None:
    normalized = str(category_code or "").strip().lower()
    if not normalized:
        return None
    return await session.scalar(select(Category.id).where(Category.code == normalized))


async def _category_subtree_ids(session: AsyncSession, root_id: UUID) -> set[UUID]:
    """ID раздела и всех вложенных подкатегорий (с защитой от циклов)."""
    found = {root_id}
    frontier = [root_id]
    for _ in range(16):
        if not frontier:
            break
        result = await session.execute(
            select(Category.id).where(Category.parent_id.in_(frontier))
        )
        children = {row[0] for row in result.all()} - found
        found |= children
        frontier = list(children)
    return found


async def search_records(
    session: AsyncSession,
    query: str,
    user: User,
    *,
    category_code: str | None = None,
    record_type: RecordType | str | None = None,
    tags: Any = None,
    date_from: date | datetime | str | None = None,
    date_to: date | datetime | str | None = None,
    exact: bool = False,
    limit: int = 20,
) -> list[Record]:
    """Ищет активные записи, доступные пользователю (ТЗ §11).

    Поиск идёт по названию, описанию, категории, подкатегории, типу, тегам,
    пользовательским текстовым полям, именам файлов и ID записи. Пароли,
    секреты и зашифрованные значения не индексируются.

    Фильтры: ``category_code`` (раздел и его подкатегории), ``record_type``,
    ``tags`` (все указанные теги), ``date_from``/``date_to`` по дате создания.
    ``exact`` включает поиск точного совпадения вместо частичного.
    """
    if limit <= 0:
        return []
    normalized = normalize_query(query)
    if not normalized:
        return []

    normalized_type = normalize_record_type(record_type)
    if record_type is not None and not isinstance(record_type, RecordType):
        value = str(record_type).strip().upper()
        if value not in {"", "ALL"} and normalized_type is None:
            # Неизвестный фильтр не должен внезапно расширять выдачу.
            return []

    category = aliased(Category)
    subcategory = aliased(Category)
    stmt = (
        select(Record)
        .join(category, category.id == Record.category_id)
        .outerjoin(subcategory, subcategory.id == Record.subcategory_id)
        .where(Record.status == "ACTIVE")
        .options(
            selectinload(Record.fields),
            selectinload(Record.tags).selectinload(RecordTag.tag),
        )
    )

    if category_code:
        category_id = await _resolve_category_id(session, category_code)
        if category_id is None:
            return []
        # Поиск в разделе охватывает и вложенные подкатегории.
        subtree_ids = await _category_subtree_ids(session, category_id)
        stmt = stmt.where(
            or_(
                Record.category_id.in_(subtree_ids),
                Record.subcategory_id.in_(subtree_ids),
            )
        )

    if normalized_type is not None:
        stmt = stmt.where(Record.record_type == normalized_type)

    for tag_name in normalize_tags(tags):
        stmt = stmt.where(_tag_exists(Tag.name == tag_name))

    moment_from = _as_datetime(date_from)
    if moment_from is not None:
        stmt = stmt.where(Record.created_at >= moment_from)
    moment_to = _as_datetime(date_to, end=True)
    if moment_to is not None:
        stmt = stmt.where(Record.created_at <= moment_to)

    if exact:
        exact_types = tuple(
            item_type
            for item_type in RecordType
            if normalized
            in {
                item_type.value.lower(),
                RECORD_TYPE_LABELS.get(item_type, "").lower(),
            }
        )
        exact_conditions = [
            func.lower(Record.title) == normalized,
            func.lower(func.coalesce(Record.description, "")) == normalized,
            func.lower(category.name) == normalized,
            func.lower(category.code) == normalized,
            func.lower(subcategory.name) == normalized,
            func.lower(subcategory.code) == normalized,
            cast(Record.id, String) == normalized,
            _tag_exists(func.lower(Tag.name) == normalized),
            _field_exists(func.lower(RecordField.value_text) == normalized),
            _file_exists(func.lower(File.original_name) == normalized),
        ]
        if exact_types:
            exact_conditions.append(Record.record_type.in_(exact_types))
        match = or_(*exact_conditions)
    else:
        token_conditions = []
        for token in tokenize_query(normalized):
            pattern = f"%{token}%"
            token_match = or_(
                Record.title.ilike(pattern),
                Record.description.ilike(pattern),
                category.name.ilike(pattern),
                category.code.ilike(pattern),
                subcategory.name.ilike(pattern),
                subcategory.code.ilike(pattern),
                cast(Record.id, String).ilike(pattern),
                _tag_exists(Tag.name.ilike(pattern)),
                _field_exists(
                    or_(
                        RecordField.value_text.ilike(pattern),
                        cast(RecordField.value_json, Text).ilike(pattern),
                    )
                ),
                _file_exists(File.original_name.ilike(pattern)),
            )
            matched_types = record_types_matching(token)
            if matched_types:
                token_match = or_(token_match, Record.record_type.in_(matched_types))
            token_conditions.append(token_match)

        match = and_(*token_conditions)
        phrase_types = record_types_for_query(normalized)
        if phrase_types:
            match = or_(match, Record.record_type.in_(phrase_types))

    stmt = stmt.where(match)

    # Релевантность: точное название, затем вхождение в название, затем дата.
    needle = f"%{normalized}%"
    stmt = stmt.order_by(
        case(
            (func.lower(Record.title) == normalized, 0),
            (Record.title.ilike(needle), 1),
            else_=2,
        ),
        Record.created_at.desc(),
        Record.id,
    )

    context, reason = await load_access_context(user, session)
    if reason is not None or context is None:
        return []

    result = await session.execute(stmt)
    visible: list[Record] = []
    for record in result.scalars().all():
        # Default Deny по каждой записи: категория, подкатегория, собственные права.
        denial = await record_denial_reason(context, record.id, PermissionCode.VIEW, session)
        if denial is None:
            visible.append(record)
        if len(visible) >= limit:
            break
    return visible


__all__ = [
    "MAX_QUERY_LENGTH",
    "RECORD_TYPE_LABELS",
    "SECRET_FIELD_KEYS",
    "normalize_query",
    "normalize_record_type",
    "normalize_tags",
    "record_types_for_query",
    "record_types_matching",
    "search_records",
    "tokenize_query",
]
