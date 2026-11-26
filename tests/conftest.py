"""
Общая конфигурация pytest: URL/логин из .env, один Chromium на сессию.
Папка тестов: tests/{PROJECT} из .env.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import allure
import pytest
from playwright.sync_api import Browser, Page, Playwright, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
_TOOLS = str(ROOT / "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)
_TESTS = str(ROOT / "tests")
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

from test_suite import document_title, project_name, suite_dirs, tests_dir  # noqa: E402
from allure_report import allure_upload_file, install_playwright_allure_hooks  # noqa: E402

install_playwright_allure_hooks()

# Не подгружать conftest чужих проектов.
_current_suite = tests_dir().name
collect_ignore = [path.name for path in suite_dirs() if path.name != _current_suite]
for extra in ("allure-results", ".venv", ".pytest_cache", "__pycache__"):
    if extra not in collect_ignore:
        collect_ignore.append(extra)


def pytest_ignore_collect(collection_path, config):  # noqa: ARG001
    path = Path(str(collection_path)).resolve()
    target = tests_dir().resolve()
    for suite in suite_dirs():
        suite = suite.resolve()
        if path == suite or suite in path.parents:
            return path != target and target not in path.parents
    return None


def pytest_report_header(config):  # noqa: ARG001
    target = tests_dir().relative_to(ROOT).as_posix()
    return [
        f"Проект: {project_name()}",
        f"Документ: {document_title()}",
        f"Стенд: {BASE_URL}",
        f"Папка тестов: {target}",
    ]


def _allure_results_dir(config) -> Path | None:
    raw = getattr(config.option, "allure_report_dir", None)
    if not raw:
        return None
    path = Path(str(raw))
    return path if path.is_absolute() else ROOT / path


def _write_allure_environment(config) -> None:
    """Allure Overview → блок Environment (environment.properties)."""
    out_dir = _allure_results_dir(config)
    if out_dir is None:
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    stand = os.environ.get("STAND", "").strip() or BASE_URL
    lines = [
        f"Project={project_name()}",
        f"Stand={stand}",
        f"Base.URL={BASE_URL}",
        f"Document={document_title()}",
        f"Browser=Chromium",
        f"Headless={'TRUE' if _is_headless() else 'FALSE'}",
    ]
    (out_dir / "environment.properties").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


@pytest.hookimpl(trylast=True)
def pytest_sessionstart(session):
    _write_allure_environment(session.config)


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


_load_dotenv()

BASE_URL = os.environ.get("BASE_URL", "").strip()
LOGIN = os.environ.get("LOGIN", "").strip()
PASSWORD = os.environ.get("PASSWORD", "").strip()
if not BASE_URL or not LOGIN or not PASSWORD:
    raise RuntimeError(
        "Задайте BASE_URL, LOGIN и PASSWORD в .env (корень проекта)."
    )


def upload_file(page: Page, path_to_file: str | Path, selector: str = 'input[type="file"]') -> None:
    """
    Загрузка файла как Selenium send_keys: без клика по полю (иначе Finder на macOS).

    path_to_file — абсолютный путь или относительно корня репо (например files/sample.xlsx).
    """
    path = Path(path_to_file)
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file():
        raise FileNotFoundError(f"Файл для загрузки не найден: {path}")
    allure_upload_file(page, path.resolve(), selector)


def _is_headless() -> bool:
    return os.environ.get("HEADLESS", "FALSE").strip().upper() == "TRUE"


@pytest.fixture(scope="session")
def base_url():
    return BASE_URL


@pytest.fixture(scope="session")
def login_credentials():
    return {"username": LOGIN, "password": PASSWORD}


@pytest.fixture(scope="session")
def playwright_instance():
    with sync_playwright() as playwright:
        yield playwright


@pytest.fixture(scope="session")
def browser(playwright_instance: Playwright) -> Browser:
    headless = _is_headless()
    launch_kwargs: dict = {"headless": headless, "args": ["--start-maximized"]}
    browser = playwright_instance.chromium.launch(**launch_kwargs)
    try:
        yield browser
    finally:
        browser.close()


@pytest.fixture
def page(browser: Browser) -> Page:
    headless = _is_headless()
    context = browser.new_context() if headless else browser.new_context(no_viewport=True)
    page = context.new_page()
    try:
        yield page
    finally:
        context.close()


@pytest.fixture(autouse=True)
def _allure_labels(request):
    """Русские названия в Allure из docstring теста и класса."""
    node = request.node
    if node.cls and node.cls.__doc__:
        feature = node.cls.__doc__.strip().splitlines()[0].strip()
        if feature:
            allure.dynamic.feature(feature)
    doc = node.function.__doc__
    if doc:
        title = doc.strip().splitlines()[0].strip()
        if title:
            allure.dynamic.title(title)
            if "TC-" in title and not node.get_closest_marker("allure_dynamic_story"):
                allure.dynamic.story(title)
    module_doc = node.module.__doc__
    if module_doc:
        epic = module_doc.strip().splitlines()[0].strip()
        if epic:
            allure.dynamic.epic(epic)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    """Скриншот при падении, если до этого не успели прикрепить."""
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or not report.failed:
        return
    page = item.funcargs.get("page")
    if page is None:
        return
    try:
        from allure_report import _attach_screenshot

        _attach_screenshot(page, "Скриншот при ошибке")
    except Exception:
        pass
