"""Скрипт ручной/автоматической проверки меню бота через Telethon.

Требуются переменные окружения:
  TG_API_ID, TG_API_HASH, TG_PHONE, BOT_USERNAME (например @millionesr_databot)
Запуск: python scripts/telegram_menu_check.py
"""
import asyncio
import os

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"Telethon не установлен: {exc}")


async def main() -> None:
    api_id = int(os.getenv("TG_API_ID", "0"))
    api_hash = os.getenv("TG_API_HASH", "")
    phone = os.getenv("TG_PHONE", "")
    bot = os.getenv("BOT_USERNAME", "")
    if not (api_id and api_hash and phone and bot):
        raise SystemExit("Задайте TG_API_ID, TG_API_HASH, TG_PHONE, BOT_USERNAME")
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        await client.start(phone=phone)
        await client.send_message(bot, "/start")
        await asyncio.sleep(3)
        messages = await client.get_messages(bot, limit=5)
        for m in messages:
            print("---")
            print(m.text)
            if m.reply_markup:
                for row in m.reply_markup.rows:
                    print(" | ".join(b.text for b in row.buttons))


if __name__ == "__main__":
    asyncio.run(main())
