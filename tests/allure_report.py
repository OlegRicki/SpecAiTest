"""
Автоматические шаги Allure для Playwright-тестов.

Подключается из conftest.py один раз на сессию: русские шаги, скриншоты,
введённые данные — без правок в каждом test_*.py.
"""

from __future__ import annotations

import re
from functools import wraps
from typing import Any, Callable, TypeVar

import allure

_F = TypeVar("_F", bound=Callable[..., Any])
_INSTALLED = False


def _attach_screenshot(page: Any, name: str = "Скриншот") -> None:
    try:
        png = page.screenshot(full_page=False)
        allure.attach(png, name, allure.attachment_type.PNG)
    except Exception:
        pass


def _attach_text(label: str, value: str) -> None:
    allure.attach(value, label, allure.attachment_type.TEXT)


def _is_sensitive(hint: str) -> bool:
    h = hint.lower()
    return any(token in h for token in ("password", "пароль", "pass", "secret", "token"))


def _humanize_selector(selector: str) -> str:
    sel = selector.strip()
    # В составном локаторе называем целевой элемент, а не родительский диалог.
    parts = [part for part in sel.split(" >> ") if not part.startswith("nth=")]
    sel = parts[-1] if parts else sel
    # input[type=…]: сначала атрибуты-подсказки (aria-label/placeholder/name),
    # затем понятное название по type — хрупкий CSS не показываем в отчёте.
    m = re.search(
        r'^input\b[^>]*type=["\'](email|password|text|file|checkbox|search|number|tel|url|month|week|date|datetime-local|time)["\']',
        sel,
    )
    if m:
        m_attr = re.search(r'\[(?:aria-label|placeholder|name)=["\']([^"\']+)["\']', sel)
        if m_attr:
            return m_attr.group(1)
        type_ru = {
            "email": "логин",
            "password": "пароль",
            "text": "текстовое поле",
            "file": "поле загрузки файла",
            "checkbox": "чекбокс",
            "search": "поисковое поле",
            "number": "числовое поле",
        }
        return type_ru.get(m.group(1), f"поле {m.group(1)}")
    m = re.search(r'\[(?:aria-label|placeholder)=["\']([^"\']+)["\']', sel)
    if m:
        return m.group(1)
    m = re.search(r'internal:(?:label|text)="((?:\\.|[^"\\])*)"', sel)
    if m:
        return m.group(1)
    m = re.search(r'\[role=["\']([^"\']+)["\']\]', sel)
    if m:
        role = m.group(1)
        role_ru = {
            "button": "кнопка",
            "textbox": "поле",
            "combobox": "выпадающий список",
            "link": "ссылка",
            "dialog": "диалог",
        }.get(role, role)
        return role_ru
    m = re.search(
        r'(?:^|[/:])role=([^[\s\]]+)(?:\[name=["\']([^"\']+)["\'][^\]]*\])?',
        sel,
    )
    if m:
        role, name = m.group(1), m.group(2)
        role_ru = {
            "button": "кнопка",
            "textbox": "поле",
            "combobox": "выпадающий список",
            "link": "ссылка",
            "dialog": "диалог",
        }.get(role, role)
        if name:
            return f"{role_ru} «{name}»"
        return role_ru
    m = re.search(r'\[?name=["\']([^"\']+)["\']', sel)
    if m:
        name = m.group(1)
        if name in {"username", "login"}:
            return "логин"
        if name == "password":
            return "пароль"
        return name
    m = re.search(r'text=["\']([^"\']+)["\']', sel)
    if m:
        return f"«{m.group(1)}»"
    if "dialog" in sel.lower():
        return "диалог"
    if len(sel) > 60:
        return sel[:57] + "..."
    return sel or "элемент"


def _locator_hint(locator: Any) -> str:
    try:
        # Sync Locator и внутренний Locator из LocatorAssertions.
        impl = getattr(locator, "_impl_obj", locator)
        selector = impl._selector  # playwright internal
        return _humanize_selector(selector)
    except Exception:
        return "элемент"


def _page_from_locator(locator: Any) -> Any | None:
    try:
        return locator.page
    except Exception:
        return None


def _step_after_action(page: Any | None) -> None:
    if page is not None:
        _attach_screenshot(page)


def _wrap_locator_method(method_name: str, step_template: str) -> _F:
    def decorator(orig: _F) -> _F:
        @wraps(orig)
        def wrapper(locator: Any, *args: Any, **kwargs: Any) -> Any:
            hint = _locator_hint(locator)
            page = _page_from_locator(locator)
            with allure.step(step_template.format(hint=hint)):
                if method_name == "fill" and args:
                    value = args[0]
                    if _is_sensitive(hint):
                        _attach_text("Введённые данные", "***")
                    else:
                        _attach_text("Введённые данные", str(value))
                elif method_name == "set_input_files" and args:
                    paths = args[0]
                    if isinstance(paths, (list, tuple)):
                        _attach_text("Файлы", "\n".join(str(p) for p in paths))
                    else:
                        _attach_text("Файл", str(paths))
                elif method_name == "press" and args:
                    _attach_text("Клавиша", str(args[0]))
                try:
                    result = orig(locator, *args, **kwargs)
                except Exception:
                    if page is not None:
                        _attach_screenshot(page, "Скриншот при ошибке действия")
                    raise
                _step_after_action(page)
                return result

        return wrapper  # type: ignore[return-value]

    return decorator


def _wrap_page_method(step_template: str, attach_url: bool = False) -> _F:
    def decorator(orig: _F) -> _F:
        @wraps(orig)
        def wrapper(page: Any, *args: Any, **kwargs: Any) -> Any:
            extra = ""
            if attach_url and args:
                extra = f": {args[0]}"
            with allure.step(step_template.format(extra=extra)):
                if attach_url and args:
                    _attach_text("URL", str(args[0]))
                result = orig(page, *args, **kwargs)
                _step_after_action(page)
                return result

        return wrapper  # type: ignore[return-value]

    return decorator


def _wrap_assertion(method_name: str, step_template: str) -> _F:
    def decorator(orig: _F) -> _F:
        @wraps(orig)
        def wrapper(assertions: Any, *args: Any, **kwargs: Any) -> Any:
            # LocatorAssertions inherits from SyncBase, which has _impl_obj
            impl = getattr(assertions, "_impl_obj", None)
            locator = None
            if impl is not None:
                locator = getattr(impl, "_actual_locator", None) or getattr(impl, "_locator", None)
            hint = _locator_hint(locator) if locator else "элемент"
            page = _page_from_locator(locator) if locator else None
            with allure.step(step_template.format(hint=hint)):
                try:
                    result = orig(assertions, *args, **kwargs)
                except Exception:
                    if page is not None:
                        _attach_screenshot(page, "Скриншот при ошибке проверки")
                    raise
                _step_after_action(page)
                return result

        return wrapper  # type: ignore[return-value]

    return decorator


def install_playwright_allure_hooks() -> None:
    """Патчит Playwright один раз: шаги Allure на действия и проверки."""
    global _INSTALLED
    if _INSTALLED:
        return

    from playwright.sync_api import Keyboard, Locator, LocatorAssertions, Mouse, Page

    Locator.fill = _wrap_locator_method("fill", "Ввести данные в «{hint}»")(Locator.fill)
    Locator.click = _wrap_locator_method("click", "Нажать «{hint}»")(Locator.click)
    Locator.press = _wrap_locator_method("press", "Нажать клавишу в «{hint}»")(Locator.press)
    Locator.clear = _wrap_locator_method("clear", "Очистить «{hint}»")(Locator.clear)
    Locator.check = _wrap_locator_method("check", "Отметить «{hint}»")(Locator.check)
    Locator.uncheck = _wrap_locator_method("uncheck", "Снять отметку «{hint}»")(Locator.uncheck)
    Locator.set_input_files = _wrap_locator_method(
        "set_input_files", "Загрузить файл в «{hint}»"
    )(Locator.set_input_files)
    Locator.select_option = _wrap_locator_method(
        "select_option", "Выбрать значение в «{hint}»"
    )(Locator.select_option)

    Page.goto = _wrap_page_method("Открыть страницу{extra}", attach_url=True)(Page.goto)

    _orig_page_click = Page.click

    def _patched_page_click(page: Any, selector: str, *args: Any, **kwargs: Any) -> Any:
        hint = _humanize_selector(selector)
        with allure.step(f"Нажать «{hint}»"):
            _attach_text("Селектор", selector)
            result = _orig_page_click(page, selector, *args, **kwargs)
            _step_after_action(page)
            return result

    Page.click = _patched_page_click  # type: ignore[method-assign]

    _orig_wait_for_load_state = Page.wait_for_load_state

    def _patched_wait_for_load_state(page: Any, state: str = "load", *args: Any, **kwargs: Any) -> Any:
        with allure.step(f"Дождаться загрузки страницы ({state})"):
            result = _orig_wait_for_load_state(page, state, *args, **kwargs)
            _step_after_action(page)
            return result

    Page.wait_for_load_state = _patched_wait_for_load_state  # type: ignore[method-assign]

    _orig_wait_for_selector = Page.wait_for_selector

    def _patched_wait_for_selector(page: Any, selector: str, *args: Any, **kwargs: Any) -> Any:
        hint = _humanize_selector(selector)
        with allure.step(f"Дождаться элемента «{hint}»"):
            _attach_text("Селектор", selector)
            result = _orig_wait_for_selector(page, selector, *args, **kwargs)
            _step_after_action(page)
            return result

    Page.wait_for_selector = _patched_wait_for_selector  # type: ignore[method-assign]

    def _mouse_click(orig: _F) -> _F:
        @wraps(orig)
        def wrapper(mouse: Any, x: float, y: float, *args: Any, **kwargs: Any) -> Any:
            page = getattr(mouse, "_page", None) or getattr(mouse, "page", None)
            with allure.step(f"Клик мышью (x={x:.0f}, y={y:.0f})"):
                _attach_text("Координаты", f"x={x}, y={y}")
                result = orig(mouse, x, y, *args, **kwargs)
                _step_after_action(page)
                return result

        return wrapper  # type: ignore[return-value]

    Mouse.click = _mouse_click(Mouse.click)

    def _keyboard_press(orig: _F) -> _F:
        @wraps(orig)
        def wrapper(keyboard: Any, key: str, *args: Any, **kwargs: Any) -> Any:
            page = getattr(keyboard, "_page", None) or getattr(keyboard, "page", None)
            with allure.step(f"Нажать клавишу «{key}»"):
                _attach_text("Клавиша", key)
                result = orig(keyboard, key, *args, **kwargs)
                _step_after_action(page)
                return result

        return wrapper  # type: ignore[return-value]

    Keyboard.press = _keyboard_press(Keyboard.press)

    LocatorAssertions.to_be_visible = _wrap_assertion(
        "to_be_visible", "Проверка: «{hint}» отображается"
    )(LocatorAssertions.to_be_visible)
    LocatorAssertions.not_to_be_visible = _wrap_assertion(
        "not_to_be_visible", "Проверка: «{hint}» не отображается"
    )(LocatorAssertions.not_to_be_visible)
    LocatorAssertions.to_be_hidden = _wrap_assertion(
        "to_be_hidden", "Проверка: «{hint}» скрыт"
    )(LocatorAssertions.to_be_hidden)
    LocatorAssertions.to_be_enabled = _wrap_assertion(
        "to_be_enabled", "Проверка: «{hint}» доступен"
    )(LocatorAssertions.to_be_enabled)
    LocatorAssertions.to_be_disabled = _wrap_assertion(
        "to_be_disabled", "Проверка: «{hint}» недоступен"
    )(LocatorAssertions.to_be_disabled)

    _INSTALLED = True


def allure_upload_file(page: Any, path_to_file: Any, selector: str = 'input[type="file"]') -> None:
    """Allure-обёртка для upload_file (вызывается из conftest)."""
    from pathlib import Path

    from playwright.sync_api import Locator

    path = Path(path_to_file)
    with allure.step(f"Загрузить файл «{path.name}»"):
        _attach_text("Файл", str(path))
        _attach_text("Селектор", selector)
        loc = page.locator(selector).first
        orig = getattr(Locator.set_input_files, "__wrapped__", Locator.set_input_files)
        orig(loc, str(path.resolve()))
        _step_after_action(page)
