"""Isolated multimodal PMI analyst; emits validated scenarios, never test code."""
from __future__ import annotations
import base64
import json
import secrets
from contextlib import contextmanager
from threading import Event, Thread
from time import monotonic
from typing import Literal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

ROOT=Path(__file__).resolve().parents[1]
PROMPT=ROOT/'agents/pmi-normalizer.md'


@contextmanager
def model_request(label, timeout, progress):
    if progress is None:
        yield
        return

    started = monotonic()
    finished = Event()

    def report_wait():
        while not finished.wait(15):
            elapsed = int(monotonic() - started)
            progress(f'{label} · ответа пока нет ({elapsed} с ожидания)')

    progress(f'{label} · отправляю запрос (таймаут {timeout} с)')
    reporter = Thread(target=report_wait, daemon=True)
    reporter.start()
    try:
        yield
    finally:
        finished.set()
        reporter.join()
    elapsed = monotonic() - started
    progress(f'{label} · ответ получен за {elapsed:.1f} с')


def check_vision(client,model,directory,timeout=60,progress=None):
    """A blind image-only token prevents silently using a text-only backend."""
    import fitz
    code=secrets.token_hex(4).upper()
    with fitz.open() as doc:
        page=doc.new_page(width=420,height=140)
        page.insert_text((35,90),code,fontsize=46,fontname='helv')
        png=page.get_pixmap().tobytes('png')
    directory.mkdir(parents=True,exist_ok=True)
    (directory/'vision-probe.png').write_bytes(png)
    with model_request('Проверка чтения изображения', timeout, progress):
        response=client.chat.completions.create(model=model,temperature=0,max_tokens=64,timeout=timeout,
            messages=[{'role':'user','content':[{'type':'text','text':'Перепишите единственный код на картинке точно. Верните только код, без пояснений.'},
                {'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(png).decode()}}]}])
    text=response.choices[0].message.content or ''
    (directory/'vision-probe-response.txt').write_text(text)
    if text.strip().strip('`').strip()!=code:
        raise ValueError('Модель не прошла проверку чтения картинки. Сценарии не публикуются; укажите другую vision-модель.')


class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid')


class Item(Strict):
    text:str=Field(min_length=1)
    kind:Literal['explicit','derived','fixture']
    source_refs:list[str]=Field(min_length=1)
    rationale:str=''


class Case(Strict):
    title:str=Field(min_length=1)
    status:Literal['ready','needs_ui','needs_clarification']
    preconditions:list[str]
    data:list[str]
    steps:list[Item]=Field(min_length=1)
    expected:list[Item]=Field(min_length=1)
    notes:list[str]
    questions:list[str]
    llm_criteria:list[str]=[]


class Coverage(Strict):
    source_id:str
    role:Literal['requirement','context','caption','image','unresolved']
    case_indices:list[int]
    explanation:str=Field(min_length=1)


class Image(Strict):
    source_id:str
    readability:Literal['clear','partial','unreadable']
    observations:list[str]=Field(min_length=1)
    ui_details:list[str]=Field(default_factory=list)
    conflicts:list[str]


class Result(Strict):
    cases:list[Case]
    coverage:list[Coverage]
    images:list[Image]


def validate(payload,bundle):
    result=Result.model_validate(payload)
    known={b['id']:b for b in bundle['blocks']}
    coverage={c.source_id:c for c in result.coverage}
    if len(coverage)!=len(result.coverage) or set(coverage)!=set(known):
        raise ValueError('coverage должна однократно учитывать каждый блок/ячейку/изображение выбранного раздела')
    for entry in result.coverage:
        if any(i<1 or i>len(result.cases) for i in entry.case_indices):raise ValueError(f'Неверный индекс cases у {entry.source_id}')
        if known[entry.source_id]['mandatory']:
            if entry.role not in {'requirement','unresolved'} or not entry.case_indices:
                raise ValueError(f'Обязательный источник пропущен: {entry.source_id}')
        if entry.role=='unresolved' and not any(result.cases[i-1].questions for i in entry.case_indices):
            raise ValueError(f'Нерешённый вопрос не сформулирован: {entry.source_id}')
    for index,case in enumerate(result.cases,1):
        if not case.title.strip() or any(not i.text.strip() for i in case.steps+case.expected):raise ValueError('Пустое название/действие/ожидание')
        if case.questions and case.status!='needs_clarification':raise ValueError(f'Кейс {index}: вопросы требуют needs_clarification')
        if case.status=='needs_clarification' and not case.questions:raise ValueError(f'Кейс {index}: нужен конкретный вопрос')
        for item in case.steps+case.expected:
            if set(item.source_refs)-set(known):raise ValueError(f'Кейс {index}: выдуманные source_refs')
            if item.kind!='explicit' and not item.rationale.strip():raise ValueError(f'Кейс {index}: уточнение без основания')
            if any(index not in coverage[s].case_indices for s in item.source_refs):
                raise ValueError(f'Кейс {index}: source_refs не согласованы с coverage')
    image_ids={i['id'] for i in bundle['images']}
    if len(result.images)!=len(image_ids) or {i.source_id for i in result.images}!=image_ids:
        raise ValueError('Нужно разобрать каждую переданную иллюстрацию')
    return result


def blocked(result):
    return not result.cases or any(c.status=='needs_clarification' for c in result.cases) or any(
        i.readability=='unreadable' or i.conflicts for i in result.images)


def messages(bundle,outline,policy='',previous_cases=None):
    text={k:v for k,v in bundle.items() if k not in {'images'}}
    text['outline']=[{'id':s.id,'printed_number':s.printed_number,'title':s.title} for s in outline]
    text['fixture_policy']=policy
    if previous_cases:
        text['previous_cases'] = previous_cases
    content=[{'type':'text','text':'Один раздел ПМИ. Технические IDs заголовков не являются печатной нумерацией Word.\n'+json.dumps(text,ensure_ascii=False)}]
    for image in bundle['images']:
        content.append({'type':'text','text':f'Исходная иллюстрация {image["id"]}, рядом с {image["near_block"]}.'})
        data=base64.b64encode(Path(image['path']).read_bytes()).decode()
        content.append({'type':'image_url','image_url':{'url':f'data:{image["mime"]};base64,{data}'}})
    instructions = PROMPT.read_text()
    if previous_cases:
        instructions += (
            '\nПри обновлении previous_cases — прежние сценарии для сравнения. '
            'Источник требований — новая редакция раздела. Сохрани названия, '
            'действия, данные, ожидания и указания для кейсов, чьи требования '
            'не менялись. Для каждого кейса укажи актуальные ссылки на источники.\n'
        )
    return [{'role':'system','content':instructions},{'role':'user','content':content}]


class NormalizationResponseError(ValueError):
    """Model answered, but no complete, validated scenario can be published."""


def normalize(client,model,bundle,outline,run_dir,policy='',attempts=2,timeout=300,progress=None,
              previous_cases=None):
    if bundle['unavailable_images']:raise ValueError('Недоступны/неподдерживаемы иллюстрации: '+', '.join(bundle['unavailable_images']))
    original = messages(bundle, outline, policy, previous_cases)
    request = original
    token_limit = 14_000
    run_dir.mkdir(parents=True,exist_ok=True)
    last=None;usage=[]
    for attempt in range(1,attempts+1):
        label = f'Нормализация раздела {bundle["section_id"]} · попытка {attempt}/{attempts}'
        with model_request(label, timeout, progress):
            response=client.chat.completions.create(model=model,messages=request,temperature=0,
                response_format={'type':'json_object'},max_tokens=token_limit,timeout=timeout)
        choice=response.choices[0];text=choice.message.content or ''
        (run_dir/f'response-{attempt}.txt').write_text(text)
        usage.append(response.usage.model_dump() if response.usage else {})
        (run_dir/'usage.json').write_text(json.dumps(usage,ensure_ascii=False,indent=2))
        try:
            if choice.finish_reason == 'length':
                raise ValueError(f'Модель исчерпала лимит ответа ({token_limit} токенов) до завершения сценария.')
            if choice.finish_reason!='stop':raise ValueError(f'Ответ не завершён: {choice.finish_reason}; сценарий не публикуется')
            result=validate(json.loads(text),bundle)
            (run_dir/'validated.json').write_text(result.model_dump_json(indent=2))
            return result
        except (ValueError,TypeError) as exc:
            last=str(exc);(run_dir/f'validation-{attempt}.txt').write_text(last)
            if attempt<attempts:
                if choice.finish_reason == 'length':
                    token_limit = 24_000
                    request = [*original, {'role':'user','content':
                        'Предыдущий ответ оборвался. Сразу сформируй полный JSON. '
                        'Формулируй пояснения кратко, сохрани все требования, проверки и ссылки на источники.'}]
                    if progress is not None:
                        progress(f'Ответ оборвался. Повторяю запрос с лимитом {token_limit} токенов.')
                else:
                    request = [*original, {'role':'assistant','content':text},{'role':'user','content':
                        'Исправь только формат/полноту ответа без изменения требований. Ошибка валидации: '+last}]
                    if progress is not None:
                        progress('Ответ не прошёл проверку формата или полноты. Запрашиваю исправление.')
    raise NormalizationResponseError('Результат не прошёл проверку: '+str(last))


def render(result,bundle,source_relative,case_ids=None,revisions=None):
    section=bundle['section_id'];status='blocked' if blocked(result) else 'ready'
    case_ids = case_ids or [f'{section}.{i}' for i in range(1, len(result.cases)+1)]
    revisions = revisions or {}
    clean=lambda value:' '.join(str(value).split())
    lines=[f'# {section}. {bundle["title"]}','',f'<!-- pmi-normalizer: {status} -->',
        '',f'> Документ: {bundle["source_name"]}',
        f'> Исходный раздел, таблицы и иллюстрации: [{section}]({source_relative})',
        f'> SHA-256 документа: `{bundle["source_sha256"]}`','',
        '> Источники и изображения не заменяют проверку текущего UI. Уточнения и синтетические данные помечены явно.','']
    if status=='blocked':lines+=['**Нормализация заблокирована:** до генерации тестов необходимо разрешить вопросы/противоречия ниже.','']
    for number,case in enumerate(result.cases,1):
        case_id = case_ids[number-1]
        lines += [f'### TC-{case_id} — {clean(case.title)}','',f'**Предусловия:**']
        lines += ['- '+clean(p) for p in case.preconditions]+['','**Данные:**']
        if case.data:
            lines+=['| Тестовые данные / правило |','|---|']
            lines+=['| '+clean(p).replace('\\','\\\\').replace('|','\\|')+' |' for p in case.data]
        lines+=['']
        for table in bundle['tables']:
            if any(number in c.case_indices and c.source_id.startswith(table['id']+'.') for c in result.coverage):
                lines += [table['markdown'],'']
        lines += ['**Шаги:**']
        lines += [f'{i}. {clean(step.text)}' for i,step in enumerate(case.steps,1)]
        lines += ['','**Ожидание:**']+['- '+clean(item.text) for item in case.expected]
        if case.llm_criteria:lines+=['','**Проверка LLM:**']+['- '+clean(p) for p in case.llm_criteria]
        lines += ['','**Примечания:**',f'- Состояние детализации: `{case.status}`.']
        if case_id in revisions:
            lines.append(f'- Ревизия требований: {revisions[case_id]}.')
        lines += ['- '+clean(p) for p in case.notes]
        lines += ['- Требует уточнения: '+clean(p) for p in case.questions]
        for kind,items in [('Шаг',case.steps),('Ожидание',case.expected)]:
            for i,item in enumerate(items,1):
                origin={'explicit':'из источника','derived':'техническое уточнение','fixture':'тестовые данные'}[item.kind]
                lines.append(f'- {kind} {i}: {origin}; источники {", ".join(item.source_refs)}. '+clean(item.rationale))
        lines+=['']
    lines+=['## Разбор иллюстраций','']
    for image in result.images:
        lines += [f'**{image.source_id}** — читаемость: `{image.readability}`.']
        lines += ['- '+clean(p) for p in image.observations]
        lines += ['- Сверить на стенде: '+clean(p) for p in image.ui_details]
        lines += ['- Расхождение: '+clean(p) for p in image.conflicts]+['']
    lines+=['## Учёт исходных требований','', '| Источник | Роль | Кейсы | Как учтено |','|---|---|---|---|']
    for entry in result.coverage:
        ids=', '.join(f'TC-{case_ids[i-1]}' for i in entry.case_indices) or '—'
        lines.append(f'| {entry.source_id} | {entry.role} | {ids} | {clean(entry.explanation).replace("|","/")} |')
    return '\n'.join(lines)+'\n'
