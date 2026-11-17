"""Единые тексты и визуальные токены OFFICE VAULT (дизайн-пакет v1.0)."""

MAIN_TITLE = "🏢 OFFICE VAULT"
MAIN_SUBTITLE = "Корпоративное хранилище данных"
MAIN_PROMPT = "Выберите раздел для работы."

EMOJI = {
    "main": "🏢",
    "bots": "🤖",
    "vpn": "🌐",
    "max": "📱",
    "general": "📁",
    "access": "🔐",
    "users": "👥",
    "roles": "🛡️",
    "requests": "📨",
    "audit": "📊",
    "settings": "⚙️",
    "back": "⬅️",
    "home": "🏠",
    "success": "✅",
    "warning": "⚠️",
    "danger": "🗑️",
    "search": "🔎",
    "profile": "👤",
}

WELCOME = (
    f"{MAIN_TITLE}\n"
    f"{MAIN_SUBTITLE}\n\n"
    "Добро пожаловать, {name}! {MAIN_PROMPT}"
)

NO_ACCESS = "🔒 У вас пока нет доступа к этому разделу."
REQUEST_SENT = "📨 Заявка отправлена. Администратор рассмотрит её и отправит уведомление о решении."
MAX_FREE = "📱 Свободные аккаунты: {count}\nНажмите «Получить», чтобы назначить аккаунт за собой."
MAX_NONE = "⚠️ Свободных аккаунтов пока нет."
MAX_ISSUED = "✅ Аккаунт успешно назначен."

COLORS = {
    "ink": "#0E1A14",
    "pine_900": "#162F26",
    "pine_800": "#1F362A",
    "moss": "#4C8F68",
    "mint": "#8FDBB6",
    "cream": "#F5EEDB",
    "sand": "#E6C98A",
    "amber": "#F2994A",
    "danger": "#E35D5B",
}
