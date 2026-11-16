from __future__ import annotations

from typing import Any
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
