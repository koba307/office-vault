"""Сервис уведомлений с учётом прав пользователей.

Уведомления никогда не должны содержать открытые секреты. Текст
предварительно очищается от строк/значений, похожих на секреты.
"""

from __future__ import annotations

import re
from typing import Iterable

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Permission, RolePermission, User, UserStatus

_SECRET_MARKERS = ("password", "secret", "token", "api_key", "пароль", "секрет", "ключ")


def sanitize_text(text: str) -> str:
    """Удаляет из текста фрагменты, похожие на секреты."""
    lines = []
    for line in (text or "").splitlines():
        lowered = line.lower()
        if any(marker in lowered for marker in _SECRET_MARKERS):
            # Не отправляем строку целиком, если она похожа на секрет.
            line = re.sub(r"(?i)(password|secret|token|api_key|пароль|секрет|ключ)\s*[:=]\s*\S+", r"\1: ***", line)
        lines.append(line)
    return "\n".join(lines)


async def active_recipients(session: AsyncSession, user_ids: Iterable) -> list[User]:
    """Возвращает активных пользователей из списка id/telegram_id."""
    ids = [str(u) for u in user_ids]
    if not ids:
        return []
    # Поддерживаем и UUID (User.id), и telegram_id.
    result = await session.execute(select(User).where(User.status == UserStatus.ACTIVE))
    users = result.scalars().all()
    selected = []
    for user in users:
        if str(user.id) in ids or str(user.telegram_id) in ids:
            selected.append(user)
    return selected


async def users_with_permission(session: AsyncSession, permission_code: str) -> list[User]:
    """Пользователи, чья роль включает указанное право."""
    result = await session.execute(
        select(User)
        .join(RolePermission, RolePermission.role_id == User.role_id, isouter=True)
        .join(Permission, Permission.id == RolePermission.permission_id, isouter=True)
        .where(User.status == UserStatus.ACTIVE, Permission.code == permission_code)
    )
    # Уникальность по id.
    seen: set = set()
    out: list[User] = []
    for user in result.scalars().all():
        if user.id in seen:
            continue
        seen.add(user.id)
        out.append(user)
    return out


async def send_notification(
    bot: Bot,
    session: AsyncSession,
    *,
    recipients: Iterable | None = None,
    permission_code: str | None = None,
    text: str,
) -> int:
    """Отправляет уведомление списку получателей или по праву.

    Возвращает количество успешно отправленных сообщений.
    """
    safe_text = sanitize_text(text)
    users: list[User] = []
    if permission_code:
        users = await users_with_permission(session, permission_code)
    elif recipients is not None:
        users = await active_recipients(session, recipients)

    sent = 0
    for user in users:
        try:
            await bot.send_message(chat_id=user.telegram_id, text=safe_text)
            sent += 1
        except Exception:  # noqa: BLE001 — недоступный чат не должен ронять рассылку
            continue
    return sent
