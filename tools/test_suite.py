"""Папки specs/ и tests/ текущего проекта из PROJECT в .env."""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_TESTS_ROOT = ROOT / "tests"
_SPECS_ROOT = ROOT / "specs"
_SKIP_TEST_DIRS = {
    "__pycache__",
    ".venv",
    ".pytest_cache",
    "allure-results",
    "allure-report",
    "screenshots",
}
_SLUG_RE = re.compile(r"[^\w\-]+", re.UNICODE)


def load_dotenv(path: Path = ROOT / ".env") -> None:
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


def project_name() -> str:
    """Имя тестируемого проекта из .env (папки specs/{name} и tests/{name})."""
    load_dotenv()
    raw = os.environ.get("PROJECT", "").strip()
    if not raw:
        raise RuntimeError(
            "Задайте PROJECT в .env (имя тестируемого проекта)."
        )
    slug = _SLUG_RE.sub("_", raw.lower()).strip("_-")
    if not slug:
        raise RuntimeError(f"Некорректный PROJECT: {raw!r}")
    return slug


def _peek_source(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".md":
        return path.read_text(encoding="utf-8", errors="ignore")[:8000]
    if suffix == ".docx":
        from docx import Document as DocxDocument

        lines: list[str] = []
        for para in DocxDocument(str(path)).paragraphs[:40]:
            text = (para.text or "").strip()
            if not text:
                continue
            style = (para.style.name or "").lower() if para.style else ""
            lines.append(f"# {text}" if "heading 1" in style else text)
        return "\n".join(lines)
    if suffix == ".pdf":
        import fitz

        doc = fitz.open(str(path))
        try:
            return doc[0].get_text() if doc.page_count else ""
        finally:
            doc.close()
    return path.read_text(encoding="utf-8", errors="ignore")[:8000]


def document_title() -> str:
    """Заголовок документа (DOCUMENT_SOURCE, INDEX.md или имя проекта)."""
    load_dotenv()
    source_value = os.environ.get("DOCUMENT_SOURCE", "").strip()
    if source_value:
        source = Path(source_value)
        if source.is_file():
            try:
                for line in _peek_source(source).splitlines():
                    text = line.strip()
                    if not text:
                        continue
                    if text.startswith("# "):
                        return text[2:].strip()
                    return text
            except Exception:
                pass
    index = document_dir() / "INDEX.md"
    if index.is_file():
        for line in index.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    return project_name()


_GENERIC_CONFTEST = '''"""Конфигурация тестов проекта (from conftest import BASE_URL / upload_file)."""

from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import Page

_ROOT = Path(__file__).resolve().parents[2]

BASE_URL = os.environ.get("BASE_URL", "").strip()
LOGIN = os.environ.get("LOGIN", "").strip()
PASSWORD = os.environ.get("PASSWORD", "").strip()


def upload_file(page: Page, path_to_file: str | Path, selector: str = 'input[type="file"]') -> None:
    """Загрузка файла без клика по полю (иначе Finder на macOS)."""
    from allure_report import allure_upload_file

    path = Path(path_to_file)
    if not path.is_absolute():
        path = _ROOT / path
    if not path.is_file():
        raise FileNotFoundError(f"Файл для загрузки не найден: {path}")
    allure_upload_file(page, path.resolve(), selector)
'''


def ensure_project() -> bool:
    """Создаёт specs/{PROJECT}/ и tests/{PROJECT}/, если такого проекта ещё не было."""
    name = project_name()
    specs = _SPECS_ROOT / name
    tests = _TESTS_ROOT / name
    created = not specs.is_dir() or not tests.is_dir()
    (specs / "scenarios").mkdir(parents=True, exist_ok=True)
    tests.mkdir(parents=True, exist_ok=True)
    conftest = tests / "conftest.py"
    if not conftest.is_file():
        conftest.write_text(_GENERIC_CONFTEST, encoding="utf-8")
        created = True
    return created


def document_dir() -> Path:
    ensure_project()
    return _SPECS_ROOT / project_name()


def tests_dir() -> Path:
    ensure_project()
    return _TESTS_ROOT / project_name()


def suite_dirs() -> list[Path]:
    """Все папки тестов проектов в tests/."""
    if not _TESTS_ROOT.is_dir():
        return []
    return [
        path
        for path in sorted(_TESTS_ROOT.iterdir())
        if path.is_dir() and path.name not in _SKIP_TEST_DIRS and not path.name.startswith(".")
    ]
