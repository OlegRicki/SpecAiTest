#!/usr/bin/env python3
"""
Парсер тест-документов → scenarios/*.md + INDEX.md + index.json

Поддерживает:
  - Markdown (.md) — основной формат
  - DOCX (.docx) — через python-docx (опционально)
  - PDF (.pdf) — через pymupdf (опционально)

Примеры:
  # путь из .env (DOCUMENT_SOURCE)
  python tools/parse_document.py
  npm run parse-document

  python tools/parse_document.py requirements.md
  python tools/parse_document.py requirements.pdf -o specs/example
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_suite import document_dir, ensure_project, project_name


SECTION_RE = re.compile(
    r"^(?P<id>\d+(?:\.\d+)*)\.\s+(?P<title>.+)$"
)
# TC-1, TC-1.1, TC-1.1.1, ТС-1.1 (кириллическая С)
CASE_RE = re.compile(
    r"^(?:TC|ТС)[-_ ]?(?P<id>\d+(?:\.\d+)*)\s*[—:\-–]?\s*(?P<title>.*)$",
    re.IGNORECASE,
)
META_LINE_RE = re.compile(
    r"^[-*]\s*(?P<key>[A-Za-zА-Яа-я0-9_ /-]+)\s*:\s*(?P<value>.+)\s*$"
)


@dataclass
class TestCase:
    id: str
    title: str
    preconditions: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    expected: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    llm_criteria: list[str] = field(default_factory=list)
    data: list[str] = field(default_factory=list)  # строки Markdown-таблиц


@dataclass
class Section:
    id: str
    title: str
    cases: list[TestCase] = field(default_factory=list)
    intro: list[str] = field(default_factory=list)


@dataclass
class Document:
    title: str
    meta: dict[str, str] = field(default_factory=dict)
    sections: list[Section] = field(default_factory=list)
    source: str = ""
    name: str = ""
    format: str = ""


def _slug(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s\-а-яё]+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[\s_]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:60] or "section"


def _clean_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        s = re.sub(r"^[-*•]\s+", "", s)
        s = re.sub(r"^\d+[.)]\s+", "", s)
        out.append(s)
    return out


def _parse_case_body(
    lines: list[str],
) -> tuple[list[str], list[str], list[str], list[str], list[str], list[str]]:
    """Разбирает тело кейса на предусловия / шаги / ожидание / LLM / прочее."""
    preconditions: list[str] = []
    steps: list[str] = []
    expected: list[str] = []
    notes: list[str] = []
    llm_criteria: list[str] = []
    data: list[str] = []
    mode = "notes"

    header_map = {
        "проверка llm": "llm",
        "проверяет llm": "llm",
        "предуслов": "preconditions",
        "precondition": "preconditions",
        "given": "preconditions",
        "шаг": "steps",
        "step": "steps",
        "действия": "steps",
        "ожида": "expected",
        "expected": "expected",
        "результат": "expected",
        "then": "expected",
        "примечан": "notes",
        "note": "notes",
    }

    for raw in lines:
        line = raw.strip()
        if not line:
            if data and data[-1]:
                data.append("")
            continue

        # Таблицы относятся к текущему кейсу, а не к шагам/ожиданиям.
        # Проверяем до заголовков: ячейка может содержать «Ожидаемый результат».
        if line.startswith("|") and line.endswith("|"):
            data.append(line)
            continue
        if line.strip("#*_: ").lower() in {"данные", "тип и данные", "data"}:
            continue

        # **Шаги:** или Шаги: или ## Шаги
        header = re.sub(r"^[#*_]+", "", line).strip().lower()
        header = header.rstrip(":").strip()
        matched_mode = None
        for key, value in header_map.items():
            if header.startswith(key) or key in header:
                # только короткие заголовки секций, не обычные шаги
                if len(header) <= 40:
                    matched_mode = value
                    break
        if matched_mode:
            mode = matched_mode
            continue

        bucket = {
            "preconditions": preconditions,
            "steps": steps,
            "expected": expected,
            "notes": notes,
            "llm": llm_criteria,
        }[mode]
        cleaned = re.sub(r"^[-*•]\s+", "", line)
        cleaned = re.sub(r"^\d+[.)]\s+", "", cleaned)
        if cleaned:
            bucket.append(cleaned)

    return preconditions, steps, expected, notes, llm_criteria, data


def parse_markdown(text: str, source: str = "") -> Document:
    lines = text.splitlines()
    doc = Document(title="Untitled", source=source)
    i = 0
    current_section: Section | None = None
    current_case_lines: list[str] = []
    current_case_header: tuple[str, str] | None = None
    in_meta = False

    def flush_case() -> None:
        nonlocal current_case_header, current_case_lines
        if not current_section or not current_case_header:
            current_case_lines = []
            current_case_header = None
            return
        case_id, case_title = current_case_header
        pre, steps, exp, notes, llm, data = _parse_case_body(current_case_lines)
        current_section.cases.append(
            TestCase(
                id=case_id,
                title=case_title or f"Case {case_id}",
                preconditions=pre,
                steps=steps,
                expected=exp,
                notes=notes,
                llm_criteria=llm,
                data=data,
            )
        )
        current_case_header = None
        current_case_lines = []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("# ") and not stripped.startswith("##"):
            flush_case()
            doc.title = stripped[2:].strip()
            in_meta = False
            i += 1
            continue

        if stripped.startswith("## "):
            flush_case()
            heading = stripped[3:].strip()
            # Meta / Метаданные
            if heading.lower() in {"meta", "мета", "метаданные", "metadata", "окружение"}:
                current_section = None
                in_meta = True
                i += 1
                continue

            in_meta = False
            m = SECTION_RE.match(heading)
            if m:
                section = Section(id=m.group("id"), title=m.group("title").strip())
            else:
                # раздел без номера
                section = Section(id=str(len(doc.sections) + 1), title=heading)
            doc.sections.append(section)
            current_section = section
            i += 1
            continue

        if stripped.startswith("### "):
            flush_case()
            in_meta = False
            heading = stripped[4:].strip()
            m = CASE_RE.match(heading)
            if not m:
                # ### без TC — считаем кейсом с авто-id
                if current_section is None:
                    current_section = Section(id="1", title="General")
                    doc.sections.append(current_section)
                auto_id = f"{current_section.id}.{len(current_section.cases) + 1}"
                current_case_header = (auto_id, heading)
            else:
                if current_section is None:
                    # кейс вне раздела — создаём раздел по первой цифре
                    major = m.group("id").split(".")[0]
                    current_section = Section(id=major, title=f"Section {major}")
                    doc.sections.append(current_section)
                title = (m.group("title") or "").strip() or f"Case {m.group('id')}"
                current_case_header = (m.group("id"), title)
            current_case_lines = []
            i += 1
            continue

        if in_meta:
            mm = META_LINE_RE.match(stripped)
            if mm:
                key = mm.group("key").strip().lower().replace(" ", "_")
                doc.meta[key] = mm.group("value").strip()
            i += 1
            continue

        if current_case_header is not None:
            current_case_lines.append(line)
        elif current_section is not None and stripped:
            current_section.intro.append(stripped)
        i += 1

    flush_case()
    return doc


def docx_to_markdown(path: Path) -> str:
    try:
        from docx import Document as DocxDocument  # type: ignore
        from docx.table import Table
    except ImportError as exc:
        raise SystemExit(
            "Для .docx установите python-docx:\n"
            "  uv add python-docx\n"
            "  uv run python tools/parse_document.py"
        ) from exc

    document = DocxDocument(str(path))
    out: list[str] = []
    for para in document.iter_inner_content():
        if isinstance(para, Table):
            rows = [
                [cell.text.replace("\\", "\\\\").replace("|", "\\|")
                 .replace("\r\n", "\n").replace("\n", "<br>") for cell in row.cells]
                for row in para.rows
            ]
            if rows:
                out.append("")
                out.append("| " + " | ".join(rows[0]) + " |")
                out.append("| " + " | ".join("---" for _ in rows[0]) + " |")
                out.extend("| " + " | ".join(row) + " |" for row in rows[1:])
                out.append("")
            continue
        text = (para.text or "").strip()
        if not text:
            continue
        style = (para.style.name or "").lower() if para.style else ""
        if "heading 1" in style:
            out.append(f"# {text}")
        elif "heading 2" in style:
            out.append(f"## {text}")
        elif "heading 3" in style:
            out.append(f"### {text}")
        elif "list" in style:
            out.append(f"- {text}")
        else:
            out.append(text)
    return "\n".join(out) + "\n"


def _pdf_line_to_markdown(
    text: str,
    size: float,
    body_size: float,
    title_done: bool,
    mode: str,
) -> tuple[str, bool, str]:
    """Превращает строку PDF в markdown. mode: body|steps|expected|preconditions|notes."""
    lower = text.lower().rstrip(":")

    header_modes = {
        "проверка llm": "llm",
        "проверяет llm": "llm",
        "предуслов": "preconditions",
        "precondition": "preconditions",
        "given": "preconditions",
        "шаг": "steps",
        "step": "steps",
        "действия": "steps",
        "ожида": "expected",
        "expected": "expected",
        "результат": "expected",
        "then": "expected",
        "примечан": "notes",
        "note": "notes",
    }
    for key, value in header_modes.items():
        if lower.startswith(key) or lower == key:
            return text, title_done, value

    if lower in {"meta", "мета", "метаданные", "окружение", "environment"}:
        return f"## {text}", title_done, "body"

    if CASE_RE.match(text) or re.match(r"^(?:TC|ТС)\b", text, re.IGNORECASE):
        return f"### {text}", True, "body"

    # Нумерованные шаги ("1. Open page") не считаем разделами внутри блока Шаги
    if SECTION_RE.match(text):
        if mode == "steps" or size < body_size + 0.8:
            return text, title_done, mode
        return f"## {text}", True, "body"

    if size >= body_size + 3.5 and not title_done:
        return f"# {text}", True, "body"
    if size >= body_size + 1.8 and mode != "steps":
        return f"## {text}", True, "body"

    return text, title_done, mode


def pdf_to_markdown(path: Path) -> str:
    try:
        import fitz  # pymupdf
    except ImportError as exc:
        raise SystemExit(
            "Для .pdf установите pymupdf:\n"
            "  uv add pymupdf\n"
            "  uv run python tools/parse_document.py"
        ) from exc

    document = fitz.open(str(path))
    sized_lines: list[tuple[str, float]] = []
    for page in document:
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans") or []
                if not spans:
                    continue
                text = "".join(span.get("text", "") for span in spans)
                text = " ".join(text.split()).strip()
                if not text:
                    continue
                size = max(float(span.get("size") or 0) for span in spans)
                sized_lines.append((text, size))
    document.close()

    if not sized_lines:
        raise SystemExit(
            f"В PDF нет извлекаемого текста: {path}\n"
            "Если это скан, нужен OCR; сохраните текстовый PDF или .docx/.md."
        )

    sizes = sorted(size for _, size in sized_lines)
    body_size = sizes[len(sizes) // 2]

    out: list[str] = []
    title_done = False
    mode = "body"
    for text, size in sized_lines:
        md_line, title_done, mode = _pdf_line_to_markdown(
            text, size, body_size, title_done, mode
        )
        out.append(md_line)
    return "\n".join(out) + "\n"


def source_to_markdown(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".md":
        return path.read_text(encoding="utf-8")
    if suffix == ".docx":
        return docx_to_markdown(path)
    if suffix == ".pdf":
        return pdf_to_markdown(path)
    raise SystemExit(f"Неподдерживаемый формат: {suffix}. Используйте .md, .docx или .pdf")


def load_document(path: Path) -> Document:
    text = source_to_markdown(path)
    document = parse_markdown(text, source=str(path))
    document.name = path.name
    document.format = path.suffix.lower().lstrip(".")
    return document


def render_case_md(case: TestCase) -> str:
    lines = [f"### TC-{case.id} {case.title}", ""]
    if case.preconditions:
        lines.append("**Предусловия:**")
        lines.extend(f"- {x}" for x in case.preconditions)
        lines.append("")
    if case.steps:
        lines.append("**Шаги:**")
        for idx, step in enumerate(case.steps, 1):
            lines.append(f"{idx}. {step}")
        lines.append("")
    if case.data:
        lines.extend(["**Данные:**", ""])
        lines.extend(case.data)
        lines.append("")
    if case.expected:
        lines.append("**Ожидание:**")
        lines.extend(f"- {x}" for x in case.expected)
        lines.append("")
    if case.llm_criteria:
        lines.append("**Проверка LLM:**")
        lines.extend(f"- {x}" for x in case.llm_criteria)
        lines.append("")
    if case.notes:
        lines.append("**Примечания:**")
        lines.extend(f"- {x}" for x in case.notes)
        lines.append("")
    return "\n".join(lines)


_BROWSER_META_KEYS = {"браузер", "browser"}


def _browser_meta(meta: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in meta.items() if key.strip().lower() in _BROWSER_META_KEYS}


def render_section_md(doc: Document, section: Section) -> str:
    lines = [
        f"# {section.id}. {section.title}",
        "",
        f"> Документ: {doc.title}",
        "",
    ]
    browser_meta = _browser_meta(doc.meta)
    if browser_meta:
        lines.append("## Meta")
        for key, value in browser_meta.items():
            lines.append(f"- {key}: {value}")
        lines.append("")
    if section.intro:
        lines.extend(section.intro)
        lines.append("")
    if not section.cases:
        lines.append("_В разделе нет распознанных тест-кейсов (TC-...)._")
        lines.append("")
    for case in section.cases:
        lines.append(render_case_md(case))
    return "\n".join(lines).rstrip() + "\n"


def render_index_md(doc: Document, section_files: dict[str, str]) -> str:
    lines = [
        f"# {doc.title}",
        "",
    ]
    browser_meta = _browser_meta(doc.meta)
    if browser_meta:
        lines.append("## Meta")
        for key, value in browser_meta.items():
            lines.append(f"- {key}: {value}")
        lines.append("")

    lines.extend(
        [
            "## Карта разделов",
            "",
            "| ID | Раздел | Кейсов | Файл | Статус |",
            "|----|--------|--------|------|--------|",
        ]
    )
    for section in doc.sections:
        rel = section_files.get(section.id, "")
        lines.append(
            f"| {section.id} | {section.title} | {len(section.cases)} | `{rel}` | todo |"
        )
    lines.append("")
    lines.append("Статусы: `todo` → `in_progress` → `done` / `blocked`")
    lines.append("")
    return "\n".join(lines)


def write_outputs(doc: Document, out_dir: Path) -> None:
    scenarios_dir = out_dir / "scenarios"
    scenarios_dir.mkdir(parents=True, exist_ok=True)

    section_files: dict[str, str] = {}
    for section in doc.sections:
        filename = f"{section.id.replace('.', '_')}_{_slug(section.title)}.md"
        rel = f"scenarios/{filename}"
        section_files[section.id] = rel
        (out_dir / rel).write_text(render_section_md(doc, section), encoding="utf-8")

    (out_dir / "INDEX.md").write_text(render_index_md(doc, section_files), encoding="utf-8")

    payload = {
        "title": doc.title,
        "source": doc.source,
        "meta": doc.meta,
        "sections": [
            {
                **asdict(section),
                "file": section_files.get(section.id),
                "status": "todo",
            }
            for section in doc.sections
        ],
    }
    (out_dir / "index.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_dotenv(path: Path = Path(".env")) -> None:
    """Подхватывает KEY=VALUE из .env без внешних зависимостей."""
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


def main(argv: list[str] | None = None) -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Парсер тест-документов → scenarios + INDEX"
    )
    parser.add_argument(
        "source",
        type=Path,
        nargs="?",
        default=None,
        help="Путь к .md/.docx/.pdf (или DOCUMENT_SOURCE в .env)",
    )
    parser.add_argument(
        "-o",
        "--out",
        type=Path,
        default=None,
        help="Каталог вывода (по умолчанию specs/{PROJECT} из .env)",
    )
    parser.add_argument(
        "--dump-md",
        type=Path,
        default=None,
        help="Сохранить промежуточный Markdown (удобно для .docx/.pdf)",
    )
    args = parser.parse_args(argv)

    source_value = args.source or os.environ.get("DOCUMENT_SOURCE", "").strip()
    if not source_value:
        print(
            "Не указан исходный документ.\n"
            "Задайте DOCUMENT_SOURCE в .env или передайте путь аргументом:\n"
            "  python tools/parse_document.py path/to/file.pdf",
            file=sys.stderr,
        )
        return 1

    source = Path(source_value)
    created = ensure_project()
    out_dir = args.out or document_dir()
    if created:
        name = project_name()
        print(f"Новый проект «{name}»: созданы specs/{name}/ и tests/{name}/")

    if not source.exists():
        print(f"Файл не найден: {source}", file=sys.stderr)
        return 1

    if args.dump_md:
        md = source_to_markdown(source)
        args.dump_md.parent.mkdir(parents=True, exist_ok=True)
        args.dump_md.write_text(md, encoding="utf-8")

    doc = load_document(source)
    if not doc.sections:
        print(
            "Не найдено ни одного раздела (## ...).\n"
            "Проверьте структуру документа: см. specs/SPEC_FORMAT.md",
            file=sys.stderr,
        )
        return 2

    total_cases = sum(len(s.cases) for s in doc.sections)
    write_outputs(doc, out_dir)

    print(f"Документ: {doc.title}")
    print(f"Источник: {source}")
    print(f"Разделов: {len(doc.sections)}")
    print(f"Кейсов:   {total_cases}")
    print(f"Вывод:    {out_dir.resolve()}")
    print(f"  - {out_dir / 'INDEX.md'}")
    print(f"  - {out_dir / 'index.json'}")
    print(f"  - {out_dir / 'scenarios'}/ ({len(doc.sections)} файлов)")
    if total_cases == 0:
        print(
            "\nПредупреждение: разделы есть, но кейсы TC-... не распознаны.\n"
            "Заголовки кейсов должны быть вида: ### TC-1.1 Название",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
