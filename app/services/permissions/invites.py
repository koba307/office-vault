from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import generate_invite_token, hash_invite_token
from app.database.models import (
    AuditLog,
    Group,
    GroupMember,
    Invite,
    PermissionCode,
    Role,
    RoleCode,
    User,
    UserStatus,
)
from app.services.permissions.service import PermissionService

# Рабочие категории соответствуют группам из seed (GROUP_HOLODKA и т.д.)
CATEGORY_GROUP_CODES: dict[str, str] = {
    "holodka": "GROUP_HOLODKA",
    "second_stage": "GROUP_SECOND_STAGE",
    "closers": "GROUP_CLOSERS",
    "general": "GROUP_GENERAL",
}


class InviteError(Exception):
    """Приглашение нельзя создать, принять или отозвать."""


class InviteService:
    """Одноразовые приглашения с ограниченным сроком действия."""

    async def create_invite(
        self,
        actor: User,
        role: RoleCode | str,
        category_codes: list[str],
        group_codes: list[str],
        ttl_hours: int,
        session: AsyncSession,
    ) -> str:
        """Создаёт приглашение и возвращает токен. Токен показывается только один раз."""
        await PermissionService.require(actor, PermissionCode.MANAGE_USERS, "system", None, session)

        try:
            role_code = RoleCode(role)
        except ValueError as exc:
            raise InviteError(f"Неизвестная роль: {role}") from exc
        if ttl_hours <= 0:
            raise InviteError("Срок действия приглашения должен быть больше нуля")

        resolved_groups = set(group_codes)
        for category_code in category_codes:
            group_code = CATEGORY_GROUP_CODES.get(category_code)
            if group_code is None:
                raise InviteError(f"Неизвестная категория: {category_code}")
            resolved_groups.add(group_code)

        if resolved_groups:
            result = await session.execute(
                select(Group.code).where(Group.code.in_(resolved_groups))
            )
            missing = resolved_groups - set(result.scalars().all())
            if missing:
                raise InviteError(f"Группы не найдены: {', '.join(sorted(missing))}")

        token = generate_invite_token()
        invite = Invite(
            token_hash=hash_invite_token(token),
            role=role_code,
            group_codes=sorted(resolved_groups),
            created_by=actor.id,
            expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours),
        )
        session.add(invite)
        await session.flush()
        session.add(
            AuditLog(
                actor_user_id=actor.id,
                action="INVITE_CREATE",
                object_type="invite",
                object_id=str(invite.id),
                metadata_={"role": role_code.value, "groups": sorted(resolved_groups)},
            )
        )
        await session.commit()
        return token

    async def redeem_invite(
        self,
        token: str,
        telegram_id: int,
        display_name: str | None,
        session: AsyncSession,
    ) -> User:
        """Активирует пользователя по токену и назначает ему роль и группы."""
        if not token:
            raise InviteError("Приглашение не найдено")
        result = await session.execute(
            select(Invite).where(Invite.token_hash == hash_invite_token(token)).with_for_update()
        )
        invite = result.scalar_one_or_none()
        if invite is None:
            raise InviteError("Приглашение не найдено")
        if invite.revoked_at is not None:
            raise InviteError("Приглашение отозвано")
        if invite.used_at is not None:
            raise InviteError("Приглашение уже использовано")
        if invite.expires_at <= datetime.now(UTC):
            raise InviteError("Срок действия приглашения истёк")

        existing = await session.execute(select(User.id).where(User.telegram_id == telegram_id))
        if existing.scalar_one_or_none() is not None:
            raise InviteError("Пользователь уже зарегистрирован")

        role_result = await session.execute(select(Role).where(Role.code == invite.role.value))
        role = role_result.scalar_one_or_none()
        if role is None:
            raise InviteError(f"Роль {invite.role.value} отсутствует в справочнике")

        user = User(
            telegram_id=telegram_id,
            display_name=display_name,
            role=RoleCode(invite.role),
            role_id=role.id,
            status=UserStatus.ACTIVE,
        )
        session.add(user)
        await session.flush()

        if invite.group_codes:
            groups_result = await session.execute(
                select(Group).where(Group.code.in_(invite.group_codes))
            )
            for group in groups_result.scalars().all():
                session.add(
                    GroupMember(group_id=group.id, user_id=user.id, assigned_by=invite.created_by)
                )

        invite.used_at = datetime.now(UTC)
        invite.used_by = user.id
        session.add(
            AuditLog(
                actor_user_id=user.id,
                action="INVITE_REDEEM",
                object_type="invite",
                object_id=str(invite.id),
            )
        )
        await session.commit()
        return user

    async def revoke_invite(self, token_id: UUID, session: AsyncSession) -> None:
        invite = await session.get(Invite, token_id)
        if invite is None:
            raise InviteError("Приглашение не найдено")
        if invite.used_at is not None:
            raise InviteError("Нельзя отозвать уже использованное приглашение")
        if invite.revoked_at is None:
            invite.revoked_at = datetime.now(UTC)
        await session.commit()

    async def list_invites(self, actor: User, session: AsyncSession) -> list[Invite]:
        """Возвращает активные приглашения без токенов."""
        await PermissionService.require(actor, PermissionCode.MANAGE_USERS, "system", None, session)
        result = await session.execute(
            select(Invite)
            .where(
                Invite.revoked_at.is_(None),
                Invite.used_at.is_(None),
                Invite.expires_at > datetime.now(UTC),
            )
            .order_by(Invite.created_at.desc())
        )
        return list(result.scalars().all())
