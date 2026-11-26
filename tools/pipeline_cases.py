"""Case-level preflight, scenario fingerprints and recoverable agent changes."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import asdict, replace
from pathlib import Path

from parse_document import parse_markdown, render_section_md


def case_document(scenario):
    return parse_markdown(scenario.read_text(), source=str(scenario))


def cases_in(scenario):
    return [c for s in case_document(scenario).sections for c in s.cases]


def fingerprint(doc, section, case):
    value = {'case': asdict(case), 'intro': section.intro, 'section': section.title, 'meta': doc.meta}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def read_state(path):
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    temp.replace(path)


def pytest_run(pipeline, targets, work, collect=False):
    # Keep each run: regression results must not overwrite the case/preflight evidence.
    attempt_dir = Path(tempfile.mkdtemp(prefix='pytest-', dir=work))
    report = attempt_dir / 'results.json'
    env = os.environ.copy()
    env['PIPELINE_PYTEST_REPORT'] = str(report)
    env['PYTHONPATH'] = str(pipeline.ROOT) + os.pathsep + env.get('PYTHONPATH', '')
    cmd = [pipeline.UV, 'run', 'python', '-m', 'pytest', '-p', 'tools.pipeline_pytest',
           '-o', 'addopts=', '-q', '--alluredir', str(attempt_dir / 'allure'), *targets]
    if collect:
        cmd.append('--collect-only')
    print('$ ' + ' '.join(cmd), flush=True)
    code = subprocess.run(cmd, cwd=pipeline.ROOT, env=env).returncode
    result = read_state(report)
    result['exitcode'] = code
    result['artifacts'] = str(attempt_dir)
    save_state(work / ('collection.json' if collect else 'results.json'), result)
    return result


def case_nodes(collection, cid):
    return [node for node, meta in collection.get('items', {}).items()
            if any(value == cid or value.startswith(cid + '.') for value in meta['ids'])]


def failure_reason(result, nodes):
    for node in nodes:
        for failure in result.get('failures', {}).get(node, {}).values():
            lines = [line.strip() for line in failure.get('message', '').splitlines() if line.strip()]
            detail = (' '.join(lines[:2])[:220] or 'без подробностей')
            return f"{failure.get('type', 'Ошибка')}: {detail}"
    return ''


def passed(result, nodes):
    return bool(nodes) and result.get('exitcode') == 0 and set(nodes).issubset(passed_nodes(result))


def passed_nodes(result):
    return {node for node, phases in result.get('reports', {}).items()
            if all(phases.get(phase) == 'passed' for phase in ('setup', 'call', 'teardown'))}


def application_blocked(answer, result, nodes):
    """Keep completed coverage only for documented application assertions, never code errors/skips."""
    if (not nodes or result.get('exitcode') != 1 or answer.get('status') != 'blocked'
            or answer.get('failure_kind') != 'application' or answer.get('coverage_complete') is not True
            or not answer.get('reason')):
        return False
    failed = set(nodes) - passed_nodes(result)
    evidence = answer.get('application_failures', {})
    if not failed or not isinstance(evidence, dict) or set(evidence) != failed:
        return False
    for node in failed:
        phases = result.get('reports', {}).get(node, {})
        detail = result.get('failures', {}).get(node, {}).get('call', {})
        observed = evidence[node]
        if (phases != {'setup': 'passed', 'call': 'failed', 'teardown': 'passed'}
                or detail.get('type') != 'AssertionError'
                or not isinstance(observed, str) or len(observed.strip()) < 8
                or observed not in detail.get('message', '')):
            return False
    return True


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in (root / 'tests').rglob('*.py')}


def backup(root, work):
    files = snapshot(root)
    folder = work / 'backup'
    for name, content in files.items():
        dest = folder / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
    (folder / 'manifest.json').write_text(json.dumps(sorted(files)))
    return files


def restore(root, files):
    current = snapshot(root)
    for name in current.keys() - files.keys():
        (root / name).unlink()
    for name, content in files.items():
        if current.get(name) == content:
            continue  # Не перезаписывать нетронутые тесты и ранее исправленные кейсы.
        dest = root / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)


def test_bodies(files):
    """AST of tests, including class decorators; helpers may change with regression checks."""
    bodies = {}
    for path, content in files.items():
        tree = ast.parse(content)
        def walk(nodes, prefix='', context=()):
            for node in nodes:
                if isinstance(node, ast.ClassDef):
                    ctx = context + tuple(ast.dump(x) for x in node.decorator_list + node.bases)
                    walk(node.body, prefix + node.name + '::', ctx)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_'):
                    bodies[path + '::' + prefix + node.name] = (context, ast.dump(node))
        walk(tree.body)
    return bodies


def shared_code(files, selected):
    """Ignore bodies of selected tests when deciding whether regression checks are needed."""
    result = {}
    for path, content in files.items():
        tree = ast.parse(content)
        def walk(nodes, prefix=''):
            for node in nodes:
                if isinstance(node, ast.ClassDef):
                    walk(node.body, prefix + node.name + '::')
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if path + '::' + prefix + node.name in selected:
                        node.body = [ast.Pass()]
        walk(tree.body)
        result[path] = ast.dump(tree)
    return result


def set_section_status(pipeline, scenario, status):
    path = pipeline.document_dir() / 'INDEX.md'
    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        if f'scenarios/{scenario.name}`' in line:
            cells = line.split('|')
            cells[-2] = f' {status} '
            lines[i] = '|'.join(cells)
    path.write_text('\n'.join(lines) + '\n')


def process_section(pipeline, scenario, only_case, timings, errors):
    doc = case_document(scenario)
    all_cases = [(s, c) for s in doc.sections for c in s.cases]
    selected = [(s, c) for s, c in all_cases if only_case is None or c.id == only_case]
    state_path = pipeline.document_dir() / '.pipeline' / 'cases.json'
    state = read_state(state_path)
    run_dir = Path(tempfile.mkdtemp(prefix='run-', dir=_mkdir(state_path.parent)))
    print(f'Артефакты и резервные копии: {run_dir}', flush=True)
    target = pipeline.tests_dir().relative_to(pipeline.ROOT).as_posix()
    for section, case in selected:
        key = f'TC-{case.id}'
        work = run_dir / key
        work.mkdir()
        digest = fingerprint(doc, section, case)
        previous = state.get(case.id, {})
        requirements_changed = bool(previous) and (
            previous.get('fingerprint') != digest or previous.get('pending_requirements_change', False))
        if not previous and any(note.startswith('Ревизия требований:') for note in case.notes):
            requirements_changed = True
        status, reason = 'blocked', ''
        code_accepted = False
        try:
            collection = pytest_run(pipeline, [target], work, collect=True)
            if collection['exitcode'] not in (0, 5):
                raise RuntimeError('Ошибка сбора тестов; код не изменён. См. вывод pytest.')
            nodes = case_nodes(collection, case.id)
            preflight = pipeline.timed_call(timings, 'Предварительная проверка тестов',
                                           pytest_run, pipeline, nodes, work) if nodes else {}
            baseline_ok = passed(preflight, nodes)
            unchanged = previous.get('fingerprint') == digest
            coverage_kept = set(previous.get('nodes', [])).issubset(nodes)
            if baseline_ok and coverage_kept and not requirements_changed and (
                    (unchanged and previous.get('status') == 'done') or not previous):
                status = 'done'
                reason = 'тесты прошли, код сохранён' + ('; первоначальная фиксация требований' if not previous else '')
            else:
                # Сравниваем с исходным состоянием, а не требуем зелёного всего проекта.
                others = sorted(set(collection.get('items', {})) - set(nodes))
                neighbor_baseline = pipeline.timed_call(timings, 'Предварительная проверка тестов',
                                                       pytest_run, pipeline, others, work) if others else {}
                if others and (neighbor_baseline.get('exitcode') not in (0, 1)
                               or not set(others).issubset(neighbor_baseline.get('reports', {}))):
                    raise RuntimeError('Не удалось получить исходные результаты соседних тестов; код не изменён')
                save_state(work / 'neighbor-baseline.json', neighbor_baseline)
                save_state(work / 'case-preflight.json', preflight)
                files = backup(pipeline.ROOT, work)
                scoped = work / 'scenario.md'
                scoped.write_text(render_section_md(doc, replace(section, cases=[case])))
                result_file = work / 'agent-result.json'
                prompt = pipeline.section_prompt(scoped)
                prompt += f'''\n\nОГРАНИЧЕНИЕ ТЕКУЩЕЙ ЗАДАЧИ (приоритет над указаниями обработать весь раздел):
Обрабатывай только {key}. Остальные кейсы не переписывай, даже в том же файле.
Сценарий и все его данные: {scoped}. Не исследуй другие кейсы без необходимости проверки общих функций.
Причина обработки: {'требования изменились' if requirements_changed else 'тесты отсутствуют, пропущены или не проходят'}.
Результат предварительного pytest текущего кейса: {work / 'case-preflight.json'}.
При дефекте приложения/недоступном стенде не ослабляй проверки. Не редактируй INDEX.md и .pipeline/cases.json.
Сохраняй ID TC в docstring каждого теста.
{'Требования изменились: разрешено удалять и заменять устаревшие варианты параметризации ТОЛЬКО текущего кейса. Реализуй все строки и режимы актуального сценария; старые значения, которых больше нет в требованиях, не сохраняй. В итоговом reason перечисли изменения состава наборов.' if requirements_changed else 'Требования не менялись: не удаляй существующие варианты параметризации ради прохождения.'}
Общие функции менять можно только при необходимости; пайплайн проверит остальные тесты после правок.
В конце запиши {result_file} как JSON: {{"status":"done" или "blocked","reason":"причина"}}.
Если актуальные тесты полностью реализованы, но выявлен подтверждённый дефект приложения, добавь failure_kind="application", coverage_complete=true и application_failures={{"точный pytest nodeid каждого падающего варианта":"дословное наблюдаемое сообщение UI/сервера, также присутствующее в AssertionError"}}. В reason опиши воспроизведение и доказательства. Не относить ошибки Python, локаторов, тайм-ауты, недоступность стенда, skip и неполную реализацию к подтверждённым дефектам приложения. Пайплайн сверит классификацию с собственным прогоном; при сомнении оставь blocked без этих полей.
Done означает полное покрытие актуального кейса; финальный pytest дополнительно выполнит пайплайн.
'''
                try:
                    code = pipeline.timed_call(timings, 'Исследование и написание кода',
                                               pipeline.run_pi_with_logs, pipeline.pi_cmd(prompt))
                    if code:
                        raise RuntimeError(f'Pi завершился с кодом {code}')
                    answer = read_state(result_file)
                    after = snapshot(pipeline.ROOT)
                    old_bodies, new_bodies = test_bodies(files), test_bodies(after)
                    selected_bases = {n.split('[')[0] for n in nodes}
                    if any(new_bodies.get(n) != body for n, body in old_bodies.items() if n not in selected_bases):
                        raise RuntimeError('Агент изменил тесты другого кейса')
                    updated = pytest_run(pipeline, [target], work, collect=True)
                    if updated['exitcode'] != 0:
                        raise RuntimeError('После правок нарушен сбор тестов')
                    new_nodes = case_nodes(updated, case.id)
                    allowed_new = {n.split('[')[0] for n in new_nodes}
                    if (new_bodies.keys() - old_bodies.keys()) - allowed_new:
                        raise RuntimeError('Агент добавил тесты другого кейса')
                    if not requirements_changed and not (set(nodes) | set(previous.get('nodes', []))).issubset(new_nodes):
                        raise RuntimeError('Удалены или переименованы существующие варианты; требуется ручная проверка')
                    if not set(others).issubset(updated.get('items', {})):
                        raise RuntimeError('Изменён набор тестов другого кейса')
                    checked = pipeline.timed_call(timings, 'Проверка изменений', pytest_run,
                                                  pipeline, new_nodes, work) if new_nodes else {}
                    app_failure = application_blocked(answer, checked, new_nodes)
                    save_state(work / 'case-verification.json', checked)
                    if not passed(checked, new_nodes) and not app_failure:
                        detail = failure_reason(checked, new_nodes) or answer.get('reason')
                        if not new_nodes:
                            detail = detail or 'нет собранных тестов с ID этого кейса'
                        message = 'Проверка кейса после работы агента не прошла'
                        raise RuntimeError(f'{message}. {detail}' if detail else message)
                    # Shared helpers, fixtures and imports may affect any existing project test.
                    if shared_code(files, selected_bases) != shared_code(after, allowed_new):
                        if others:
                            regression = pipeline.timed_call(timings, 'Проверка изменений', pytest_run,
                                                             pipeline, others, work)
                            if regression.get('exitcode') not in (0, 1) or not set(others).issubset(regression.get('reports', {})):
                                raise RuntimeError('Регрессионный прогон не завершён; изменения текущего кейса отменены')
                            regressions = passed_nodes(neighbor_baseline) - passed_nodes(regression)
                            save_state(work / 'regressions.json', {'new_failures': sorted(regressions)})
                            if regressions:
                                raise RuntimeError('Новые падения ранее проходивших тестов: ' + ', '.join(sorted(regressions)))
                            if regression['exitcode']:
                                print(f'{key}: у соседних тестов остались прежние проблемы; это не причина отката текущего кейса.', flush=True)
                    if answer.get('status') != 'done' and not app_failure:
                        raise RuntimeError(answer.get('reason') or 'Агент не подтвердил завершение кейса')
                    status, reason = ('blocked', 'Актуальные тесты сохранены; дефект приложения: ' + answer['reason']) if app_failure else ('done', 'обновлён и проверен')
                    code_accepted = True
                    if app_failure:
                        errors[key] = reason
                    nodes = new_nodes
                except BaseException:
                    restore(pipeline.ROOT, files)
                    print(f'{key}: отменены изменения текущего кейса (включая затронутые им общие функции); ранее обработанные кейсы сохранены. Копия: {work / "backup"}', flush=True)
                    raise
        except Exception as exc:
            reason = f'{type(exc).__name__}: {exc}'
            errors[key] = reason
        state[case.id] = {'fingerprint': digest, 'status': status, 'reason': reason,
                          'pending_requirements_change': requirements_changed and not (status == 'done' or code_accepted),
                          'code_accepted': code_accepted or status == 'done',
                          'nodes': nodes if status == 'done' or code_accepted else previous.get('nodes', [])}
        save_state(state_path, state)
        print(f'{key}: {status} — {reason}', flush=True)
    statuses = [state.get(c.id, {}).get('status', 'todo')
                if state.get(c.id, {}).get('fingerprint') == fingerprint(doc, s, c) else 'todo'
                for s, c in all_cases]
    set_section_status(pipeline, scenario, 'done' if all(s == 'done' for s in statuses)
                       else 'blocked' if 'blocked' in statuses else 'todo')


def _mkdir(path):
    path.mkdir(parents=True, exist_ok=True)
    return path
