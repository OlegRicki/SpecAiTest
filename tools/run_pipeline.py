#!/usr/bin/env python3
"""
Один pipeline: parse-document → Pi по каждому разделу (живой вывод) → pytest.

Запуск:
  npm run pipeline
  uv run python tools/run_pipeline.py
  uv run python tools/run_pipeline.py --skip-parse
  uv run python tools/run_pipeline.py --section 1_авторизация
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_suite import document_dir, document_title, ensure_project, project_name, tests_dir
import pipeline_cases
from pipeline_preflight import check_pi_model, check_pmi_model


ROOT = Path(__file__).resolve().parents[1]
UV = "uv"


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


def is_headless() -> bool:
    return os.environ.get("HEADLESS", "FALSE").strip().upper() == "TRUE"


def banner(title: str) -> None:
    line = "=" * 72
    print(f"\n{line}\n{title}\n{line}\n", flush=True)


def run(cmd: list[str], cwd: Path | None = None, env_overrides: dict[str, str] | None = None) -> int:
    """Запуск с живым stdout/stderr (как в чате), без буферизации вывода."""
    print(f"$ {' '.join(cmd[:8])}{' …' if len(cmd) > 8 else ''}", flush=True)
    env = os.environ.copy()
    env.update(env_overrides or {})
    env.setdefault("PYTHONUNBUFFERED", "1")
    proc = subprocess.run(cmd, cwd=str(cwd or ROOT), env=env)
    return proc.returncode


def _short(text: object, limit: int = 240) -> str:
    s = str(text).replace("\n", " ").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def format_pi_json_line(line: str) -> str | None:
    """Превращает JSON-событие Pi в читаемую строку лога."""
    line = line.strip()
    if not line:
        return None
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return line

    et = ev.get("type")
    if et == "session":
        return f"[session] {ev.get('id', '')}"
    if et == "agent_start":
        return "[agent] start"
    if et == "agent_end":
        return "[agent] end"
    if et == "turn_start":
        return "[turn] start"
    if et == "turn_end":
        return "[turn] end"
    if et == "tool_execution_start":
        name = ev.get("toolName") or ev.get("tool_name") or "?"
        args = _short(ev.get("args", {}))
        return f"[tool] ▶ {name} {args}"
    if et == "tool_execution_end":
        name = ev.get("toolName") or ev.get("tool_name") or "?"
        err = ev.get("isError") or ev.get("is_error")
        mark = "✗" if err else "✓"
        result = _short(ev.get("result", ""), 180)
        return f"[tool] {mark} {name} {result}"
    if et == "message_update":
        ame = ev.get("assistantMessageEvent") or {}
        if ame.get("type") == "text_delta":
            delta = ame.get("delta") or ""
            return delta if delta else None
        return None
    if et == "message_end":
        msg = ev.get("message") or {}
        if msg.get("role") == "assistant":
            return "\n"
        return None
    if et in {"auto_retry_start", "auto_retry_end", "compaction_start", "compaction_end"}:
        return f"[{et}] {_short(ev, 200)}"
    return None


def run_pi_with_logs(cmd: list[str]) -> int:
    """
    Запуск Pi с потоковым логом.
    --mode json даёт события инструментов/текста в реальном времени
    (ближе к чату, чем режим -p, который часто печатает только итог).
    """
    print(f"$ {' '.join(cmd[:10])} …", flush=True)
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    try:
        for raw in proc.stdout:
            formatted = format_pi_json_line(raw)
            if formatted is None:
                continue
            # text_delta печатаем без перевода строки — как стрим в чате
            if formatted == "\n":
                print(flush=True)
            elif formatted.startswith("["):
                print(f"\n{formatted}", flush=True)
            else:
                print(formatted, end="", flush=True)
        print(flush=True)
    finally:
        proc.wait()
    return proc.returncode or 0


def list_scenarios(only: str | None = None, only_todo: bool = True) -> list[Path]:
    scenarios = document_dir() / "scenarios"
    index_path = document_dir() / "INDEX.md"
    if not scenarios.is_dir():
        return []

    todo_files: set[str] | None = None
    if only_todo and index_path.is_file():
        todo_files = set()
        for line in index_path.read_text(encoding="utf-8").splitlines():
            if "| todo |" not in line and "| in_progress |" not in line:
                continue
            m = re.search(r"`(scenarios/[^`]+)`", line)
            if m:
                todo_files.add(Path(m.group(1)).name)

    files = sorted(scenarios.glob("*.md"))
    if index_path.is_file():
        current_files = {Path(file).name for _, _, file, _ in section_statuses()}
        files = [f for f in files if f.name in current_files]
    if only:
        files = [f for f in files if only in f.name]
    if todo_files is not None:
        files = [f for f in files if f.name in todo_files]
    return files


def section_statuses() -> list[tuple[str, str, str, str]]:
    """ID, название, файл и статус всех разделов из INDEX текущего проекта."""
    index = document_dir() / "INDEX.md"
    rows = []
    if index.is_file():
        for line in index.read_text(encoding="utf-8").splitlines():
            cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
            if len(cells) == 5 and cells[3].startswith("scenarios/"):
                rows.append((cells[0], cells[1], cells[3], cells[4]))
    return rows


def timed_call(timings: dict, stage: str, fn, *args, **kwargs):
    started = time.monotonic()
    timings.setdefault(stage, 0.0)
    try:
        return fn(*args, **kwargs)
    finally:
        timings[stage] += time.monotonic() - started


def duration(seconds: float) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def section_prompt(scenario: Path) -> str:
    rel = scenario.relative_to(ROOT).as_posix()
    tests_rel = tests_dir().relative_to(ROOT).as_posix()
    index_rel = (document_dir() / "INDEX.md").relative_to(ROOT).as_posix()
    title = document_title()
    project = project_name()
    headless = is_headless()
    browser_rule = (
        "Браузер: headless (без окна). HEADLESS=TRUE. Во всех исследовательских скриптах и CLI запрещены headless=False и --headed, включая диагностику."
        if headless
        else "Браузер: ВИДИМЫЙ. HEADLESS=FALSE. Во всех запусках Chromium обязательны headless=False, args=['--start-maximized'] и browser.new_context(no_viewport=True), без фиксированного viewport."
    )
    browser_rule += " Режим читать из окружения, как tests/conftest.py, не переопределять. Если заданный режим мешает проверке, сообщить об ограничении."
    upload_rule = (
        "Загрузка файлов: `upload_file(page, path)` / set_input_files, "
        "без клика по input[type=file] и системному диалогу."
    )
    return f"""Обработай раздел сценариев: {rel}
Проект: {project}
Документ: {title}

Следуй AGENTS.md и specs/TEST_WRITING.md.
В самих автотестах ограничь прохождение каждого сценария с конкретным набором параметров двумя попытками за один запуск pytest: первый запуск + максимум один повтор после сбоя. Учитывай вложенные обёртки и pytest rerun, не умножай попытки. После успеха не повторяй; после второй неудачи заверши тест ошибкой с диагностикой. Если сценарий запрещает повтор — только одна попытка. Число исследовательских запусков и повторных запусков pytest при разработке не ограничено; внутри каждого запуска автотеста лимит сохраняется.
{browser_rule}

1) Для существующих тестов сначала сравни код с актуальным сценарием и сразу сохрани исправления, не требующие исследования UI: номера шагов, лимиты ожиданий и повторов, удаление лишних проверок. Не откладывай все правки до завершения исследования. Затем исследуй неизвестные элементы и состояния сценариев в браузере (Playwright) в режиме выше. Для каждого параметризованного сценария сначала исследуй один представительный набор с наиболее полным набором полей и действий во всех применимых режимах. Остальные наборы сравни по требованиям; дополнительно исследуй только неизвестные отличия. Предварительно проходить каждую комбинацию не требуется; количество исследовательских запусков не ограничено. Сохраняй изменения по мере работы. В тестах и исследовательских скриптах жди состояние с конечным тайм-аутом вместо длительного sleep; time.sleep принимает секунды, page.wait_for_timeout — миллисекунды. При блокирующей проблеме сохрани исправления и укажи причину blocked, не заменяй обязательную проверку пропуском или фиктивным успехом.
2) Напиши или обнови нехрупкие pytest-тесты в {tests_rel}/ для всех предусмотренных сценарием комбинаций параметров и режимов, а не только исследованного набора.
   Используй три слоя: {tests_rel}/locators/ хранит только поиск элементов, {tests_rel}/pages/ — классы Page Object с методами действий и ожиданий по страницам или частям UI. В test_*.py оставь данные, явные шаги Allure по сценарию и вызовы высокоуровневых методов. Например, в тесте вызови auth.sign_in(...), а заполнение полей и клик реализуй в pages/auth_page.py через locators/auth.py. При правках одного кейса не переделывай соседние кейсы.
   Сверь существующие тесты с актуальным сценарием и таблицами этого раздела исходного документа. Наличие теста не означает полное покрытие. Исправь пропущенные поля, наборы данных и проверки. Не исключай поля на основании предположений; если требуемое поле отсутствует, тест должен падать с названием поля и шаблона.
   Если в сценарии есть блок «Проверка LLM:» — UI отдельно, смысл ответа через `assert_chat_quality` (`@pytest.mark.llm`). Без LLM — skip + тег Allure llm-unavailable. Кейсы без этого блока не менять.
3) В docstring тестов указывай ID вида «TC-x.y — …». Проверь и исправь Allure во всех тестах текущего раздела, включая существующие: явные allure.step с номерами и названиями шагов актуального сценария, конкретные кнопки/поля/ожидания, автоматические действия вложены. Каждая попытка — отдельная группа «Попытка N/M»; скриншот и фактическое состояние сохранять при сбое до повтора. Не выдавай предполагаемую причину за установленную.
4) Запусти uv run pytest -q, передав конкретные затронутые test_*.py из {tests_rel}/, а не всю папку проекта. Проверь все предусмотренные комбинации параметров и режимов. При падении исследуй конкретную проблему. Просмотри соответствующие allure-results/*-result.json: понятные названия, вложенность попыток и диагностика падений. Исправляй ошибки тестов, но не ослабляй требования ради прохождения при дефекте приложения.
5) Сверь все строки таблиц с параметрами, действиями и ожиданиями тестов. Обнови статус раздела в {index_rel}: done только после полной сверки и успешных проверок; иначе blocked с причиной в итоговом сообщении. При дефекте приложения сохрани тест с диагностикой. INDEX — только карта разделов, без источника и учёток.
6) В конце закрой все запущенные браузеры (`browser.close()`), не оставляй окна Chromium.

{upload_rule}

Работай только с проектом {project} и этим разделом. Не трогай папки других проектов.
Не пересоздавай тесты других разделов без необходимости.
"""


def pi_model_command() -> list[str]:
    model = os.environ.get("LLM_MODEL", "").strip()
    provider = os.environ.get("LLM_PROVIDER", "").strip()
    if not model or not provider:
        raise ValueError("Задайте LLM_PROVIDER и LLM_MODEL для Pi в .env.")
    pi_bin = "pi"
    local = ROOT / "node_modules" / ".bin" / "pi"
    if local.exists():
        pi_bin = str(local)
    return [pi_bin, "--provider", provider, "--model", model]


def pi_cmd(prompt: str) -> list[str]:
    return [*pi_model_command(), "--approve", "--mode", "json", prompt]


def preflight_models(use_pmi, pmi_model, use_pi):
    if not use_pmi and not use_pi:
        return
    banner("0/3 ПРОВЕРКА ДОСТУПА К МОДЕЛЯМ")
    if use_pmi:
        print(f"ПМИ · проверяю {pmi_model or 'модель из настроек'}", flush=True)
        try:
            check_pmi_model(pmi_model)
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError(f"Субагент ПМИ: {exc}") from exc
        print(f"ПМИ · модель {pmi_model} доступна", flush=True)
    if use_pi:
        command = pi_model_command()
        print(f"Pi · проверяю {command[-1]} через {command[-3]}", flush=True)
        try:
            check_pi_model(command, ROOT)
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError(f"Агент тестов Pi: {exc}") from exc
        print(f"Pi · модель {command[-1]} доступна", flush=True)


def pmi_block_reason(scenario, stale):
    if stale:
        return "Изменилось предусловие из другого пункта ПМИ. Повторите нормализацию раздела."
    if not scenario.is_file():
        return "Файл сценария отсутствует."
    text = scenario.read_text()
    if "<!-- pmi-normalizer: blocked -->" not in text:
        return None
    questions = re.findall(r"(?m)^- Требует уточнения: (.+)$", text)
    issues = questions or re.findall(r"(?m)^- Расхождение: (.+)$", text)
    if not issues and "читаемость: `unreadable`" in text:
        issues = ["Один из скриншотов ПМИ не читается."]
    if not issues:
        return "ПМИ содержит неуточнённые требования или не удалось выделить проверяемый сценарий."
    lines = list(dict.fromkeys(issues))
    brief = " / ".join(_short(line, 210) for line in lines[:3])
    return f"ПМИ требует уточнения. {brief}" + (f" (ещё {len(lines) - 3} в Markdown)" if len(lines) > 3 else "")


def record_final_failures(result, errors, code):
    for node, phases in result.get("reports", {}).items():
        if "failed" not in phases.values():
            continue
        ids = result.get("items", {}).get(node, {}).get("ids", [])
        reason = pipeline_cases.failure_reason(result, [node]) or "Тест завершился ошибкой."
        for key in [f"TC-{cid}" for cid in ids] or [node]:
            errors[key] = errors.get(key) or "Финальный pytest: " + reason
    if code and not any("Финальный pytest:" in reason for reason in errors.values()):
        label = "Тесты не найдены." if code == 5 else "Подробности в выводе pytest или в журнале кейса."
        errors["Финальный pytest"] = label


def run_final_pytest(timings, target, errors):
    with tempfile.TemporaryDirectory(prefix="pipeline-final-") as directory:
        report = Path(directory) / "results.json"
        env = {"PIPELINE_PYTEST_REPORT": str(report)}
        code = timed_call(timings, "Финальный прогон тестов", run,
                          [UV, "run", "python", "-m", "pytest", "-p", "tools.pipeline_pytest", "-q", target],
                          cwd=ROOT, env_overrides=env)
        record_final_failures(pipeline_cases.read_state(report), errors, code)
        return code


def pmi_selection(args: argparse.Namespace) -> str | None:
    """Map the test selection to a technical section ID for the PMI normalizer."""
    if args.pmi_section:
        return args.pmi_section
    if args.case:
        return args.case.rsplit(".", 1)[0]
    if not args.section:
        return None
    ids = {
        section_id for section_id, _, file, _ in section_statuses()
        if args.section in Path(file).name
    }
    if len(ids) == 1:
        return ids.pop()
    if not ids and re.fullmatch(r"\d+(?:[._]\d+)*_?", args.section):
        return args.section.rstrip("_").replace("_", ".")
    raise ValueError("Для нормализации укажите технический ID раздела через --pmi-section.")


def _run_pipeline(argv: list[str] | None, timings: dict, section_errors: dict[str, str]) -> int:
    load_dotenv()
    os.chdir(ROOT)

    parser = argparse.ArgumentParser(description="ПМИ или обычный парсер → Pi → pytest")
    parser.add_argument("--project", help="Проект вместо PROJECT из окружения")
    parser.add_argument("--source", type=Path, help="Документ вместо DOCUMENT_SOURCE из окружения")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--skip-parse", action="store_true", help="Использовать готовые сценарии")
    mode.add_argument("--pmi", action="store_true", help="Подготовить сценарии субагентом ПМИ")
    parser.add_argument("--pmi-section", help="Технический ID раздела ПМИ, например 3.4")
    parser.add_argument("--pmi-model", help="Модель субагента вместо PMI_MODEL")
    parser.add_argument("--skip-pi", action="store_true", help="Только parse + pytest")
    parser.add_argument("--skip-pytest", action="store_true", help="Не запускать финальный pytest")
    parser.add_argument("--case", help="Один кейс, например 5.4 или TC-5.4 (все его параметры)")
    parser.add_argument("--continue-on-blocked", action="store_true",
                        help="Для совместимости: продолжение после проблем раздела теперь включено всегда")
    parser.add_argument(
        "--section",
        default=None,
        help="Подстрока имени файла сценария, повторная обработка независимо от статуса",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Все scenarios/*.md, даже со статусом done",
    )
    args = parser.parse_args(argv)
    if args.skip_parse and (args.pmi_section or args.pmi_model):
        parser.error("--pmi-section и --pmi-model требуют запуска разбора ПМИ")
    if args.project:
        os.environ["PROJECT"] = args.project
    if args.source:
        os.environ["DOCUMENT_SOURCE"] = str(args.source.resolve())
    if args.case:
        args.case = args.case.removeprefix("TC-")
        if not re.fullmatch(r"\d+(?:\.\d+)+", args.case):
            parser.error("--case: ожидается ID, например 5.4 или TC-5.4")

    project = project_name()
    created = ensure_project()
    if created:
        print(f"Новый проект «{project}»: созданы specs/{project}/ и tests/{project}/", flush=True)
    title = document_title()
    normalized_project = any((document_dir() / name).exists()
                             for name in ("normalization.json", "normalization-policy.md"))
    use_pmi = not args.skip_parse and bool(
        args.pmi or args.pmi_section or args.pmi_model or normalized_project)
    pmi_blocked = False
    pmi_section = None
    pmi_failed_ids = set()
    pmi_model = args.pmi_model or os.getenv("PMI_MODEL", "").strip() or os.getenv("LLM_MODEL", "").strip()
    if use_pmi:
        try:
            pmi_section = pmi_selection(args)
        except ValueError as exc:
            parser.error(str(exc))
    if use_pmi or not args.skip_pi:
        try:
            timed_call(timings, "Проверка моделей", preflight_models, use_pmi, pmi_model, not args.skip_pi)
        except (OSError, RuntimeError, ValueError) as exc:
            section_errors["Проверка моделей"] = str(exc)
            print(f"Запуск остановлен. {short_diagnostic(str(exc))}", flush=True)
            return 2
    if not args.skip_parse:
        banner(f"1/3 {'ПМИ: СУБАГЕНТ' if use_pmi else 'PARSE DOCUMENT'} — {project}")
        print(f"Проект: {project}", flush=True)
        print(f"Документ: {title}", flush=True)
        print(f"INDEX: {(document_dir() / 'INDEX.md').relative_to(ROOT).as_posix()}", flush=True)
        print(f"Тесты: {tests_dir().relative_to(ROOT).as_posix()}", flush=True)
        if use_pmi:
            command = [sys.executable, "tools/normalize_pmi.py", "--project", project]
            if pmi_section:
                command += ["--section", pmi_section]
            else:
                command.append("--all")
            if pmi_model:
                command += ["--model", pmi_model]
            with tempfile.TemporaryDirectory(prefix="pipeline-pmi-") as directory:
                report = Path(directory) / "error.json"
                code = timed_call(timings, "Парсер", run, command,
                                  env_overrides={"PIPELINE_PMI_REPORT": str(report)})
                failure = pipeline_cases.read_state(report)
            if code not in (0, 1):
                if code != 130:
                    label = f"PMI-{failure['section']}" if failure.get("section") else "Разбор ПМИ"
                    detail = failure.get("message") or f"Субагент завершился с кодом {code}."
                    section_errors[label] = f"{failure.get('title') or ''}. {detail}".lstrip(". ")
                return code
            pmi_blocked = code == 1
            for item in failure.get("failures", []):
                sid = item["section"]
                pmi_failed_ids.add(sid)
                section_errors[f"PMI-{sid}"] = f"{item['title']}. {item['message']}"
            if pmi_blocked:
                print("Часть сценариев ПМИ требует уточнений. Продолжаю с готовыми разделами.",
                      flush=True)
        else:
            code = timed_call(timings, "Парсер", run, [UV, "run", "python", "tools/parse_document.py"])
            if code != 0:
                return code
        title = document_title()

    scenarios = list_scenarios(
        only=args.section, only_todo=not (args.all or args.section or args.case or args.pmi_section))
    normalized = pipeline_cases.read_state(document_dir() / 'normalization.json').get('sections', {})
    stale_scenarios = {Path(entry['file']).name for entry in normalized.values()
                       if entry.get('stale_dependency')}
    if pmi_blocked or not args.skip_pi:
        for sid, _, file, status in section_statuses():
            if status != "blocked" or args.section and args.section not in Path(file).name:
                continue
            if args.case and args.case.rsplit(".", 1)[0] != sid:
                continue
            if pmi_section and sid != pmi_section and not sid.startswith(pmi_section + "."):
                continue
            scenario = document_dir() / file
            reason = pmi_block_reason(scenario, scenario.name in stale_scenarios)
            if reason:
                section_errors[scenario.name] = reason
    if pmi_section:
        scoped_files = {Path(file).name for section_id, _, file, _ in section_statuses()
                        if section_id == pmi_section or section_id.startswith(pmi_section + ".")}
        scenarios = [scenario for scenario in scenarios if scenario.name in scoped_files]
    if pmi_failed_ids:
        failed_files = {Path(file).name for sid, _, file, _ in section_statuses()
                        if sid in pmi_failed_ids}
        scenarios = [scenario for scenario in scenarios if scenario.name not in failed_files]
        if args.case and args.case.rsplit(".", 1)[0] in pmi_failed_ids:
            return 1
    if args.case:
        scenarios = [s for s in scenarios if any(c.id == args.case for c in pipeline_cases.cases_in(s))]
        if len(scenarios) != 1:
            print(f"Кейс TC-{args.case} не найден однозначно в выбранных разделах.", flush=True)
            return 2
    if not scenarios and not args.skip_pi:
        banner("Нет разделов для Pi")
        print(
            "Нет scenarios со статусом todo/in_progress.\n"
            "Проверьте INDEX.md текущего документа или запустите с --all / --section …",
            flush=True,
        )
        if not args.skip_pytest and pmi_failed_ids:
            print("Финальный pytest отложен: часть сценариев ПМИ не обновилась.", flush=True)
        if not args.skip_pytest and not pmi_failed_ids:
            banner(f"PYTEST — {project}")
            target = tests_dir().relative_to(ROOT).as_posix()
            print(f"Проект: {project}", flush=True)
            print(f"Документ: {title}", flush=True)
            print(f"Папка тестов: {target}", flush=True)
            code = run_final_pytest(timings, target, section_errors)
            return code or int(pmi_blocked or bool(section_errors))
        return int(pmi_blocked or bool(section_errors))

    if not args.skip_pi:
        total = len(scenarios)
        for i, scenario in enumerate(scenarios, 1):
            banner(f"2/3 PI SECTION {i}/{total}: {project} / {scenario.name}")
            reason = pmi_block_reason(scenario, scenario.name in stale_scenarios)
            if reason:
                section_errors[scenario.name] = reason
                print(reason, flush=True)
                continue
            try:
                if pipeline_cases.cases_in(scenario):
                    pipeline_cases.process_section(sys.modules[__name__], scenario, args.case, timings, section_errors)
                    continue
                code = timed_call(timings, "Исследование и написание кода", run_pi_with_logs, pi_cmd(section_prompt(scenario)))
                statuses = {Path(file).name: status for _, _, file, status in section_statuses()}
                status = statuses.get(scenario.name)
                if code != 0:
                    section_errors[scenario.name] = f"Pi завершился с кодом {code}"
                elif status != "done":
                    reason = "раздел заблокирован" if status == "blocked" else "обработка раздела не завершена"
                    section_errors[scenario.name] = f"{reason} (статус: {status or 'отсутствует'})"
            except Exception as exc:
                section_errors[scenario.name] = f"{type(exc).__name__}: {exc}"
            if scenario.name in section_errors:
                print(f"Раздел {scenario.name}: {section_errors[scenario.name]}; продолжаем остальные разделы.", flush=True)

    if not args.skip_pytest and pmi_failed_ids:
        print("Финальный pytest отложен: часть сценариев ПМИ не обновилась.", flush=True)
    if not args.skip_pytest and not pmi_failed_ids:
        banner(f"3/3 PYTEST — {project}")
        target = tests_dir().relative_to(ROOT).as_posix()
        print(f"Проект: {project}", flush=True)
        print(f"Документ: {title}", flush=True)
        print(f"Папка тестов: {target}", flush=True)
        if args.case:
            work = pipeline_cases._mkdir(document_dir() / '.pipeline' / 'final')
            collection = pipeline_cases.pytest_run(sys.modules[__name__], [target], work, collect=True)
            nodes = pipeline_cases.case_nodes(collection, args.case)
            if collection['exitcode'] != 0 or not nodes:
                section_errors[f"TC-{args.case}"] = "Не удалось собрать тесты для финального прогона."
                return 1
            result = timed_call(timings, "Финальный прогон тестов", pipeline_cases.pytest_run,
                                sys.modules[__name__], nodes, work)
            code = 0 if pipeline_cases.passed(result, nodes) else 1
            record_final_failures(result, section_errors, code)
        else:
            code = run_final_pytest(timings, target, section_errors)
        if code != 0:
            return code

    blocked = bool(section_errors) or pmi_blocked
    banner(f"PIPELINE {'BLOCKED' if blocked else 'DONE'} — {project}")
    return int(blocked)


def print_section_problems(rows, errors):
    if not errors:
        return
    remaining = dict(errors)
    print("Проблемы обработки разделов в этом запуске:", flush=True)
    for sid, title, file, _ in rows:
        section_problem = remaining.pop(Path(file).name, None)
        pmi_problem = remaining.pop(f"PMI-{sid}", None)
        case_problems = [
            f"{key} — {remaining.pop(key)}" for key in errors
            if key.startswith("TC-") and key[3:].rsplit(".", 1)[0] == sid
        ]
        problems = [p for p in (section_problem, pmi_problem, *case_problems) if p]
        if problems:
            print(f"  {sid}. {title}", flush=True)
            for problem in problems:
                print(f"    - {short_diagnostic(problem)}", flush=True)
    for name, reason in remaining.items():
        print(f"  {name} — {short_diagnostic(reason)}", flush=True)


def short_diagnostic(reason):
    for key in ("PMI_API_KEY", "OPENAI_API_KEY", "LOGIN", "PASSWORD"):
        secret = os.getenv(key)
        if secret:
            reason = reason.replace(secret, "[скрыто]")
    return _short(" ".join(reason.split()), 700)


def main(argv: list[str] | None = None) -> int:
    started = time.monotonic()
    timings: dict[str, float] = {}
    section_errors: dict[str, str] = {}
    code = None
    try:
        code = _run_pipeline(argv, timings, section_errors)
        return code
    finally:
        total = time.monotonic() - started
        banner("ИТОГ PIPELINE")
        print(f"Завершение: {'прервано / исключение' if code is None else f'код {code}'}", flush=True)
        if "Проверка моделей" in timings:
            print(f"Проверка моделей: {duration(timings['Проверка моделей'])}", flush=True)
        for stage in ("Парсер", "Предварительная проверка тестов", "Исследование и написание кода", "Проверка изменений", "Финальный прогон тестов"):
            print(f"{stage}: {duration(timings[stage]) if stage in timings else 'не запускался'}", flush=True)
        print(f"Общее время: {duration(total)}", flush=True)
        print("Время исследования включает локальные проверки pytest, запускаемые агентом.", flush=True)
        rows = []
        try:
            print(f"Разделы PROJECT={project_name()} — статусы из INDEX (не результаты финального pytest):", flush=True)
            rows = section_statuses()
            for sid, title, file, status in rows:
                failed = (Path(file).name in section_errors or f"PMI-{sid}" in section_errors or any(
                    key.startswith("TC-") and key[3:].rsplit(".", 1)[0] == sid for key in section_errors))
                result = f"ошибка обработки (INDEX: {status})" if failed else status
                print(f"  {sid}. {title}: {result}", flush=True)
            if not rows:
                print("  Карта разделов отсутствует или пуста.", flush=True)
        except Exception as exc:
            print(f"Не удалось прочитать статусы разделов: {exc}", flush=True)
        print_section_problems(rows, section_errors)


if __name__ == "__main__":
    raise SystemExit(main())
