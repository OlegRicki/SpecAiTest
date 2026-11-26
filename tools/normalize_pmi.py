#!/usr/bin/env python3
"""PMI sub-agent: one DOCX section + images -> validated Markdown scenarios."""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from dataclasses import asdict
from datetime import datetime,timezone
from pathlib import Path

from docx import Document
from pmi_source import ROOT, outline, select_sections, extract, slug
from pmi_agent import PROMPT, NormalizationResponseError, normalize, render, blocked, check_vision
from pmi_revision import digest, references, cosmetic_candidates, confirm_cosmetic, affected_cases
from parse_document import parse_markdown
from pipeline_cases import fingerprint
from test_suite import load_dotenv, ensure_project


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path,value):
    temp=path.with_suffix(path.suffix+'.tmp');temp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');temp.replace(path)


def read_json(path):
    return json.loads(path.read_text())


def parse_cases(text):
    doc = parse_markdown(text)
    return {case.id: (case, fingerprint(doc, section, case))
            for section in doc.sections for case in section.cases}


def case_contract(case):
    notes = [line for line in case.notes
             if not re.match(r'^(?:Ревизия требований:|(?:Шаг|Ожидание) \d+:)', line)]
    return case.title, case.preconditions, case.data, case.steps, case.expected, case.llm_criteria, notes


def revised_cases(previous, candidate, section_id, affected, used_ids):
    """Retain IDs for matching cases, including cases whose requirements changed."""
    available = set(previous)
    ids = []
    for case, _ in candidate.values():
        exact = [i for i in sorted(available) if case_contract(previous[i][0]) == case_contract(case)]
        titled = [i for i in sorted(available) if previous[i][0].title == case.title]
        matches = exact or titled
        match = matches[0] if len(matches) == 1 else None
        ids.append(match)
        if match:
            available.remove(match)
    if len(available) == ids.count(None) == 1:
        ids[ids.index(None)] = available.pop()
    removed_with_tests = available & used_ids
    if removed_with_tests:
        raise ValueError('В новой редакции не найдены кейсы с существующими тестами: '
                         + ', '.join(sorted(removed_with_tests)) + '. Нужна ручная сверка соответствия.')
    numbers = [int(i.rsplit('.', 1)[-1]) for i in previous.keys() | used_ids
               if i.rpartition('.')[0] == section_id]
    next_number = max(numbers, default=0) + 1
    changed = set()
    order = {i: n for n, i in enumerate(previous, 1)}
    for pos, (case, _) in enumerate(candidate.values()):
        match = ids[pos]
        if match is None:
            match = ids[pos] = f'{section_id}.{next_number}'
            next_number += 1
        if (match not in previous or case_contract(previous[match][0]) != case_contract(case)
                or affected is None or order[match] in affected):
            changed.add(match)
    return ids, changed


def publish(out,bundle,result,run_dir,state,model,source_dir,*,previous=None,affected=None,
            dependencies=None,used_ids=None,metadata=None,invalidate_dependents=False):
    folder=out/'scenarios';folder.mkdir(exist_ok=True)
    name=bundle['section_id'].replace('.','_')+'_'+slug(bundle['title'])+'.md'
    target=out/previous['file'] if previous else folder/name
    if target.exists() and previous is None:
        raise ValueError(f'Сценарий уже существует: {target}.')
    relative=os.path.relpath(source_dir/'source.md',folder)
    text=render(result,bundle,relative)
    candidate = parse_cases(text)
    if len(candidate)!=len(result.cases):raise ValueError('Markdown не совместим с текущим парсером сценариев')
    revisions = {}
    pipeline_path = out / '.pipeline' / 'cases.json'
    pipeline_state = read_json(pipeline_path) if pipeline_path.exists() else {}
    if previous:
        old_text = target.read_text()
        old_cases = parse_cases(old_text)
        ids, changed = revised_cases(old_cases, candidate, bundle['section_id'], affected,
                                     pipeline_state.keys() | (used_ids or set()))
        revisions = {i: rev for i, rev in previous.get('case_revisions', {}).items() if i in ids}
        revisions.update({i: run_dir.parent.name for i in changed})
        text = render(result, bundle, relative, ids, revisions)
        (run_dir/'previous-scenario.md').write_text(old_text)
        print('Кейсы к перепроверке:', ', '.join(f'TC-{i}' for i in ids if i in changed) or 'нет', flush=True)
    cases_with_hashes = parse_cases(text)
    cases = [asdict(c) for c, _ in cases_with_hashes.values()]
    status='blocked' if blocked(result) else 'todo'
    if previous and not blocked(result):
        for i, (_, new_fingerprint) in cases_with_hashes.items():
            old_state = pipeline_state.get(i)
            if (i not in changed and old_state and old_state.get('fingerprint') == old_cases[i][1]):
                old_state['fingerprint'] = new_fingerprint
        if not changed:
            status = previous['status']
    temp = target.with_suffix('.md.tmp')
    temp.write_text(text)
    temp.replace(target)
    if previous and pipeline_path.exists():
        write_json(pipeline_path, pipeline_state)
    entry={'id':bundle['section_id'],'title':bundle['title'],'file':'scenarios/'+target.name,'status':status,
           'cases':cases,'model':model,'scenario_sha256':sha(target),'run_directory':str(run_dir.relative_to(out)),
           'bundle':str((source_dir/'bundle.json').relative_to(out)) if source_dir.is_relative_to(out) else str(source_dir/'bundle.json'),
           'case_revisions':revisions,'dependencies':dependencies or {},
           'normalization_status':'needs_clarification' if blocked(result) else 'needs_ui' if any(c.status=='needs_ui' for c in result.cases) else 'ready'}
    entry.update(metadata or {})
    state['sections'][entry['id']]=entry
    if invalidate_dependents:
        for dependent in state['sections'].values():
            if dependent['id'] != entry['id'] and entry_depends_on(dependent, entry['id'], digest(bundle)):
                dependent['stale_dependency'] = True
                dependent['status'] = 'blocked'
                print(f'Раздел {dependent["id"]} зависит от {entry["id"]}; нужна повторная нормализация.', flush=True)
    write_json(out/'normalization.json',state)
    write_index(out,state)
    return target


def prior_bundle(out, entry):
    if 'bundle' in entry:
        return read_json(out / entry['bundle'])
    manifest = read_json(out / entry['run_directory'] / 'manifest.json')
    return read_json(out/'source'/manifest['source_sha256'][:12]/entry['id'].replace('.', '_')/'bundle.json')


def sync_statuses(out, state):
    index = out / 'INDEX.md'
    if not index.exists():
        return
    statuses = {}
    for line in index.read_text().splitlines():
        cells = [cell.strip().strip('`') for cell in line.strip().strip('|').split('|')]
        if len(cells) == 5 and cells[3].startswith('scenarios/'):
            statuses[cells[3]] = cells[4]
    for entry in state['sections'].values():
        if entry['file'] in statuses:
            entry['status'] = statuses[entry['file']]


def dependency_snapshot(source, document, sections, section, bundle, chain=()):
    """Hash explicit Word cross-references, including transitive prerequisites."""
    results = {}
    for number in sorted(references(bundle)):
        candidates = [item for item in sections if item.printed_number == number]
        if len(candidates) != 1 or candidates[0].id in (*chain, section.id):
            results[number] = None
            continue
        target = candidates[0]
        with tempfile.TemporaryDirectory(prefix='pmi-dependency-') as directory:
            dependency = extract(source, document, target, directory)
        results[number] = {'id':target.id, 'digest':digest(dependency),
                           'references': dependency_snapshot(source, document, sections, target,
                                                             dependency, (*chain, section.id))}
    return results


def same_dependencies(before, after, entries):
    if before.keys() != after.keys():
        return False
    for number, new in after.items():
        old = before[number]
        if old == new:
            continue
        if old is None or new is None or old['id'] != new['id']:
            return False
        aliases = entries.get(new['id'], {}).get('equivalent_revisions', [])
        if (old['digest'] != new['digest'] and not any(
                old['digest'] in group and new['digest'] in group for group in aliases)):
            return False
        if not same_dependencies(old['references'], new['references'], entries):
            return False
    return True


def entry_depends_on(entry, section_id, current_digest):
    def contains(mapping):
        return any(ref and ((ref['id'] == section_id and ref['digest'] != current_digest)
                            or contains(ref['references']))
                   for ref in mapping.values())
    return contains(entry.get('dependencies', {}))


def existing_test_ids(project):
    root = ROOT / 'tests' / project
    return {cid for path in root.rglob('test_*.py')
            for cid in re.findall(r'\bTC-(\d+(?:\.\d+)+)\b', path.read_text())}


def write_index(out,state):
    entries=sorted(state['sections'].values(),key=lambda e:tuple(map(int,e['id'].split('.'))))
    safe=lambda text:str(text).replace('|','/').replace('\n',' ')
    lines=[f'# {safe(state["title"])}','','## Карта разделов','',
           '| ID | Раздел | Кейсов | Файл | Статус |','|----|--------|--------|------|--------|']
    for e in entries:lines.append(f'| {e["id"]} | {safe(e["title"])} | {len(e["cases"])} | `{e["file"]}` | {e["status"]} |')
    lines+=['','Статусы: `todo` → `in_progress` → `done` / `blocked`','']
    (out/'INDEX.md').write_text('\n'.join(lines))
    write_json(out/'index.json',{'title':state['title'],'source':state['source'],'meta':{},'sections':entries})


def main(argv=None):
    load_dotenv()
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=None)
    p.add_argument('--project',default=None)
    selection = p.add_mutually_exclusive_group(required=True)
    selection.add_argument('--list',action='store_true',help='Только заголовки и технические IDs, без модели')
    selection.add_argument('--section',help='Один раздел или его подразделы, например 3.1 либо 3')
    selection.add_argument('--all',action='store_true',help='Все разделы по порядку, отдельным запросом на раздел')
    p.add_argument('--prepare-only',action='store_true',help='Извлечь исходный раздел/таблицы/картинки без модели')
    p.add_argument('--model',default=None,help='Vision-модель, либо PMI_MODEL в окружении; картинки не отбрасываются')
    p.add_argument('--policy',type=Path,help='Уточнения автора (по умолчанию specs/<project>/normalization-policy.md, если файл есть)')
    p.add_argument('--timeout',type=int,default=300)
    p.add_argument('--attempts',type=int,choices=(1,2),default=2)
    args=p.parse_args(argv)

    def progress(message):
        print(message, flush=True)

    active_section = None
    failed_sections = []
    report = os.getenv('PIPELINE_PMI_REPORT')
    try:
        source=(args.source or Path(os.environ.get('DOCUMENT_SOURCE',''))).resolve()
        if not source.is_file() or source.suffix.lower()!='.docx':raise ValueError('Укажите --source с исходным .docx')
        document=Document(source);sections=outline(document)
        if args.list:
            print('Технические IDs по структуре документа; печатные ссылки Word проверяются отдельно.')
            for s in sections:print(s.id,f'(Word: {s.printed_number or "не определён"})',s.title)
            return 0
        project=(args.project or os.environ.get('PROJECT','')).strip().lower()
        if not re.fullmatch(r'[\w-]+',project):raise ValueError('Укажите безопасное имя --project (например example)')
        if args.timeout<1:raise ValueError('--timeout должен быть положительным')
        out=ROOT/'specs'/project;out.mkdir(parents=True,exist_ok=True)
        previous=os.environ.get('PROJECT');os.environ['PROJECT']=project
        try:ensure_project()
        finally:
            if previous is None:os.environ.pop('PROJECT',None)
            else:os.environ['PROJECT']=previous
        source_hash = sha(source)
        project_policy = out / 'normalization-policy.md'
        policy_path = args.policy or (project_policy if project_policy.is_file() else None)
        policy = policy_path.read_text() if policy_path else ''
        policy_hash=hashlib.sha256(policy.encode()).hexdigest();prompt_hash=sha(PROMPT)
        with (out/'.pmi.lock').open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError(f'Нормализатор PROJECT={project} уже работает')
            state_path=out/'normalization.json'
            state=json.loads(state_path.read_text()) if state_path.exists() else None
            if state is None and any((out/'scenarios').glob('*.md')):
                raise ValueError('В проекте уже есть обычные Markdown-сценарии. Для их передачи используйте pipeline --skip-parse. '
                                 'Для отдельного разбора исходного DOCX выберите новое имя --project.')
            state=state or {'title':next((p.text.strip() for p in document.paragraphs if p.text.strip()),source.stem),
                'source':str(source),'source_sha256':source_hash,'prompt_sha256':prompt_hash,'policy_sha256':policy_hash,'sections':{}}
            sync_statuses(out, state)
            if not (out/'INDEX.md').exists():write_index(out,state)
            if args.all:
                roots = [section for section in sections if not any(
                    parent.start < section.start < parent.end for parent in sections)]
                selected = [item for root in roots for item in select_sections(sections, root.id)]
            else:
                selected = select_sections(sections, args.section)
            requested_model=args.model or os.environ.get('PMI_MODEL','').strip()
            client=None;vision_checked=False
            for s in selected:
                active_section = s
                print(f'PROJECT={project} · PMI SECTION={s.id} · {s.title}',flush=True)
                old = state['sections'].get(s.id) if not args.prepare_only else None
                previous_bundle = None
                if old:
                    file = out / old['file']
                    if not file.exists() or sha(file) != old['scenario_sha256']:
                        raise ValueError('Опубликованный сценарий изменён вручную; автоматическая перезапись запрещена')
                    previous_bundle = prior_bundle(out, old)
                    if previous_bundle['title'] != s.title and any(
                            item.id != s.id and item.title == previous_bundle['title'] for item in sections):
                        raise ValueError('Раздел сменил ID в новой редакции. Сверьте привязку существующих кейсов вручную.')
                source_dir=out/'source'/source_hash[:12]/s.id.replace('.','_')
                bundle=extract(source,document,s,source_dir)
                print(f'Блоков {len(bundle["blocks"])}; изображений {len(bundle["images"])}; таблиц {len(bundle["tables"])}; недоступных изображений {len(bundle["unavailable_images"])}',flush=True)
                if args.prepare_only:
                    print('Пакет раздела:',source_dir/'source.md',flush=True);continue
                dependencies = dependency_snapshot(source, document, sections, s, bundle)
                previous_dependencies = old.get('dependencies') if old else None
                if old and previous_dependencies is None:
                    previous_dependencies = (dependencies if previous_bundle['source_sha256'] == source_hash
                                             else {number: None for number in references(previous_bundle)})
                dependency_changed = bool(old and (old.get('stale_dependency') or not same_dependencies(
                    previous_dependencies, dependencies, state['sections'])))
                config_changed = bool(old and (
                    old.get('prompt_sha256', state['prompt_sha256']) != prompt_hash
                    or old.get('policy_sha256', state['policy_sha256']) != policy_hash))
                source_changed = bool(old and digest(previous_bundle) != digest(bundle))
                metadata = {'bundle':str((source_dir/'bundle.json').relative_to(out)),
                            'prompt_sha256':prompt_hash, 'policy_sha256':policy_hash,
                            'dependencies':dependencies}
                if old and not (source_changed or config_changed or dependency_changed):
                    old.update(metadata)
                    write_json(state_path, state)
                    print('Раздел и зависимости без изменений. Сохранён:',file,flush=True)
                    continue
                model = requested_model or (old['model'] if old else '')
                if not model:
                    if bundle['images']:raise ValueError('Нужна модель с изображениями: --model <vision-model> либо PMI_MODEL. Текстовая модель автоматически не подставляется.')
                    model=os.environ.get('LLM_MODEL','').strip()
                if not model:raise ValueError('Укажите --model или PMI_MODEL')
                if client is None:
                    from openai import OpenAI
                    base=os.environ.get('PMI_BASE_URL') or os.environ.get('OPENAI_BASE_URL')
                    key=os.environ.get('PMI_API_KEY') or os.environ.get('OPENAI_API_KEY')
                    if not base or not key:raise ValueError('Нужны PMI_BASE_URL/PMI_API_KEY либо OPENAI_BASE_URL/OPENAI_API_KEY')
                    client=OpenAI(base_url=base,api_key=key,timeout=args.timeout,max_retries=0)
                stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
                run_dir=out/'normalization-runs'/stamp/s.id.replace('.','_');run_dir.mkdir(parents=True)
                write_json(run_dir/'manifest.json',{'project':project,'section':s.id,'source_sha256':source_hash,
                    'prompt_sha256':prompt_hash,'model':model,'image_ids':[i['id'] for i in bundle['images']],
                    'policy_sha256':policy_hash,'attempts':args.attempts,
                    'dependency_changed':dependency_changed})
                if source_changed and not config_changed and not dependency_changed and old['normalization_status'] != 'needs_clarification':
                    changes = cosmetic_candidates(previous_bundle, bundle)
                    if changes and confirm_cosmetic(client, model, previous_bundle, changes, args.timeout,
                                                    progress, run_dir/'cosmetic-review.json'):
                        old_digest, new_digest = digest(previous_bundle), digest(bundle)
                        aliases = old.setdefault('equivalent_revisions', [])
                        group = next((group for group in aliases if old_digest in group), None)
                        if group is None:
                            aliases.append([old_digest, new_digest])
                        elif new_digest not in group:
                            group.append(new_digest)
                        old.update(metadata)
                        write_json(state_path, state)
                        print('Косметическая правка. Сценарий и статус сохранены:', file, flush=True)
                        continue
                affected = None
                if source_changed and not config_changed and not dependency_changed:
                    previous_result = out / old['run_directory'] / 'validated.json'
                    if previous_result.exists():
                        coverage_bundle = prior_bundle(out, {k:v for k,v in old.items() if k != 'bundle'})
                        if digest(coverage_bundle) == digest(previous_bundle):
                            affected = affected_cases(coverage_bundle, bundle, read_json(previous_result))
                if old:
                    progress('Изменились требования, зависимости или правила обработки. Сверяю кейсы.')
                if bundle['images'] and vision_checked != model:
                    check_vision(client,model,run_dir,min(args.timeout,60),progress=progress)
                    vision_checked=model
                    progress('Модель прошла проверку чтения изображения.')
                try:
                    result=normalize(client,model,bundle,sections,run_dir,policy,args.attempts,args.timeout,
                                     progress=progress,previous_cases=old['cases'] if old else None)
                except NormalizationResponseError as exc:
                    failed_sections.append({'section':s.id, 'title':s.title, 'message':str(exc)})
                    progress(f'ПМИ {s.id}: {exc} Перехожу к следующему разделу.')
                    continue
                progress('Ответ прошёл проверку. Сохраняю Markdown.')
                target=publish(out,bundle,result,run_dir,state,model,source_dir,previous=old,
                               affected=affected,dependencies=dependencies,used_ids=existing_test_ids(project),
                               metadata=metadata,invalidate_dependents=bool(old and (source_changed or dependency_changed)))
                print('Markdown:',target,flush=True)
                print('Статус:',state['sections'][s.id]['normalization_status'],flush=True)
            if failed_sections and report:
                write_json(Path(report), {'failures':failed_sections})
            return int(bool(failed_sections) or any(
                state['sections'].get(s.id,{}).get('status')=='blocked' for s in selected))
    except KeyboardInterrupt:print('Остановлено; опубликованные разделы сохранены.',file=sys.stderr);return 130
    except Exception as exc:
        message=str(exc)
        for name in ('PMI_API_KEY','OPENAI_API_KEY','PASSWORD','LOGIN'):
            if os.environ.get(name):message=message.replace(os.environ[name],'[redacted]')
        if report:
            try:
                write_json(Path(report), {
                    'section':active_section.id if active_section else None,
                    'title':active_section.title if active_section else None,
                    'message':message,
                })
            except OSError:
                pass
        print(f'Ошибка нормализации: {message}',file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
