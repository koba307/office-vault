from __future__ import annotations

from typing import Any, ClassVar

from aiogram.fsm.context import FSMContext

NAV_STACK_KEY = "nav_stack"
MAX_DEPTH = 50


class NavigationManager:
    """Управление стеком навигации в FSM (per user/chat)."""

    @staticmethod
    async def push_menu(state: FSMContext, menu_id: str, params: dict[str, Any] | None = None) -> None:
        data = await state.get_data()
        stack = list(data.get(NAV_STACK_KEY) or [])
        entry = {"menu": menu_id, "params": params or {}}
        if stack and stack[-1].get("menu") == menu_id and stack[-1].get("params") == entry["params"]:
            return  # prevent duplicates
        stack.append(entry)
        if len(stack) > MAX_DEPTH:
            stack = stack[-MAX_DEPTH:]
        await state.update_data(**{NAV_STACK_KEY: stack, "current_menu": menu_id, "menu_params": entry["params"]})

    @staticmethod
    async def get_current_menu(state: FSMContext) -> str | None:
        data = await state.get_data()
        stack = data.get(NAV_STACK_KEY) or []
        if stack:
            return stack[-1]["menu"]
        return data.get("current_menu")

    @staticmethod
    async def get_previous_menu(state: FSMContext) -> str | None:
        data = await state.get_data()
        stack = data.get(NAV_STACK_KEY) or []
        return stack[-2]["menu"] if len(stack) >= 2 else None

    @staticmethod
    async def go_back(state: FSMContext) -> str | None:
        data = await state.get_data()
        stack = list(data.get(NAV_STACK_KEY) or [])
        if len(stack) <= 1:
            await state.update_data(**{NAV_STACK_KEY: [], "current_menu": "main", "menu_params": {}})
            return "main"
        stack.pop()
        current = stack[-1]
        await state.update_data(**{NAV_STACK_KEY: stack, "current_menu": current["menu"], "menu_params": current["params"]})
        return current["menu"]

    @staticmethod
    async def go_to_main_menu(state: FSMContext) -> None:
        await state.update_data(**{NAV_STACK_KEY: [], "current_menu": "main", "menu_params": {}})

    @staticmethod
    async def clear_navigation(state: FSMContext) -> None:
        await state.update_data(**{NAV_STACK_KEY: [], "current_menu": None, "menu_params": {}})

    @staticmethod
    async def restore_menu_state(state: FSMContext, menu_id: str, params: dict[str, Any] | None = None) -> None:
        await state.update_data(current_menu=menu_id, menu_params=params or {})


class GlobalNavigation:
    """Простой стек навигации в памяти, общий для всех обработчиков."""

    _stacks: ClassVar[dict[tuple[int, int], list[str]]] = {}

    @classmethod
    def _key(cls, user_id: int, chat_id: int) -> tuple[int, int]:
        return (user_id, chat_id)

    @classmethod
    def push(cls, user_id: int, chat_id: int, screen: str) -> None:
        key = cls._key(user_id, chat_id)
        stack = cls._stacks.setdefault(key, [])
        if not stack or stack[-1] != screen:
            stack.append(screen)

    @classmethod
    def back(cls, user_id: int, chat_id: int) -> str | None:
        key = cls._key(user_id, chat_id)
        stack = cls._stacks.get(key, [])
        if len(stack) <= 1:
            cls._stacks[key] = []
            return None
        stack.pop()
        return stack[-1]

    @classmethod
    def clear(cls, user_id: int, chat_id: int) -> None:
        cls._stacks[cls._key(user_id, chat_id)] = []
