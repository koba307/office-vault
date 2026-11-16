from __future__ import annotations

import asyncio
from collections.abc import Callable

from aiogram import Bot


def render_progress_bar(percent: float, length: int = 20) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = int(length * percent / 100)
    return "━" * filled + "░" * (length - filled) + f" {int(percent)}%"


class ProgressManager:
    def __init__(self, activation_delay: int = 30, update_interval: int = 2, bar_length: int = 20) -> None:
        self.activation_delay = activation_delay
        self.update_interval = update_interval
        self.bar_length = bar_length
        self._tasks: dict[str, asyncio.Task] = {}

    async def _updater(self, bot: Bot, chat_id: int, message_id: int, render: Callable[[], str], stop_event: asyncio.Event) -> None:
        await asyncio.sleep(self.activation_delay)
        while not stop_event.is_set():
            try:
                await bot.edit_message_text(render(), chat_id=chat_id, message_id=message_id)
            except Exception:  # noqa: BLE001, S112
                continue
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.update_interval)
            except TimeoutError:
                continue

    def start(self, bot: Bot, chat_id: int, message_id: int, render: Callable[[], str]) -> asyncio.Event:
        stop_event = asyncio.Event()
        task = asyncio.create_task(self._updater(bot, chat_id, message_id, render, stop_event))
        self._tasks[str(message_id)] = task
        # attach stop_event to task for external stop
        task.stop_event = stop_event  # type: ignore[attr-defined]
        return stop_event

    async def stop(self, message_id: int) -> None:
        task = self._tasks.pop(str(message_id), None)
        if task is not None and hasattr(task, "stop_event"):
            task.stop_event.set()  # type: ignore[attr-defined]
        if task is not None:
            task.cancel()
            with __import__("contextlib").suppress(asyncio.CancelledError, Exception):
                await task
