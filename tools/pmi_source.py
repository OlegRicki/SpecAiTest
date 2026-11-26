"""Read one DOCX section with ordered text, cells and embedded images."""
from __future__ import annotations
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

ROOT=Path(__file__).resolve().parents[1]
ACTION=re.compile(r'провер|убед|удостовер|долж|наж|выполн|созд|загруз|выбр|выбер|ввест|введ|перей|отобраз',re.I)


def slug(text):return re.sub(r'[^\w-]+','_',text.lower()).strip('_')[:65]


@dataclass
class Section:
    id:str
    title:str
    level:int
    start:int
    end:int
    printed_number:str|None=None


class WordNumbers:
    """Decimal OOXML labels; unsupported formats remain explicitly unknown."""
    def __init__(self,document):
        self.root=document.part.numbering_part.element
        self.counts={}

    def label(self,p):
        props=p._p.find(qn('w:pPr'));number=props.find(qn('w:numPr')) if props is not None else None
        style=p.style
        while number is None and style is not None:
            props=style.element.find(qn('w:pPr'));number=props.find(qn('w:numPr')) if props is not None else None
            style=style.base_style
        if number is None:return None
        value=lambda parent,name,default=None:parent.find(qn('w:'+name)).get(qn('w:val')) if parent is not None and parent.find(qn('w:'+name)) is not None else default
        num_id=value(number,'numId');depth=int(value(number,'ilvl','0'))
        num=next((n for n in self.root.findall(qn('w:num')) if n.get(qn('w:numId'))==num_id),None)
        abstract_id=value(num,'abstractNumId')
        abstract=next((n for n in self.root.findall(qn('w:abstractNum')) if n.get(qn('w:abstractNumId'))==abstract_id),None)
        if abstract is None:return None
        levels={int(n.get(qn('w:ilvl'))):n for n in abstract.findall(qn('w:lvl'))}
        starts={k:int(value(v,'start','1')) for k,v in levels.items()}
        for override in num.findall(qn('w:lvlOverride')):
            k=int(override.get(qn('w:ilvl')))
            if override.find(qn('w:lvl')) is not None:levels[k]=override.find(qn('w:lvl'))
            starts[k]=int(value(override,'startOverride',str(starts.get(k,1))))
        counters=self.counts.setdefault(num_id,{})
        counters[depth]=counters.get(depth,starts.get(depth,1)-1)+1
        self.counts[num_id]={k:v for k,v in counters.items() if k<=depth}
        pattern=value(levels.get(depth),'lvlText')
        if not pattern:return None
        used=[int(n)-1 for n in re.findall(r'%(\d+)',pattern)]
        if any(value(levels.get(k),'numFmt')!='decimal' for k in used):return None
        return re.sub(r'%(\d+)',lambda m:str(counters.get(int(m[1])-1,starts.get(int(m[1])-1,1))),pattern).rstrip('. ')


def level(paragraph):
    style=paragraph.style
    visited=set()
    while style is not None and style.style_id not in visited:
        visited.add(style.style_id)
        match=re.search(r'(?:heading|заголовок)\s*(\d+)',style.name,re.I)
        if match:return int(match[1])
        style=style.base_style
    return None


def outline(document):
    """Only heading text; section IDs describe structure, not printed Word numbers."""
    headings=[];counters={};numbers=WordNumbers(document)
    for pos,node in enumerate(document.element.body):
        if node.tag!=qn('w:p'):continue
        p=Paragraph(node,document);printed=numbers.label(p);depth=level(p)
        if not depth or not p.text.strip():continue
        if depth==1:continue  # document/chapter title
        counters[depth]=counters.get(depth,0)+1
        counters={k:v for k,v in counters.items() if k<=depth}
        identifier='.'.join(str(counters.get(k,1)) for k in range(2,depth+1))
        headings.append((identifier,p.text.strip(),depth,pos,printed))
    if not headings:raise ValueError('В DOCX не найдены разделы со стилями Heading 2/3; нужна разметка заголовков.')
    return [Section(i,t,l,p,next((n[3] for n in headings[j+1:] if n[2]<=l),len(document.element.body)),printed)
            for j,(i,t,l,p,printed) in enumerate(headings)]


def select_sections(sections,selector):
    found=[s for s in sections if s.id==selector]
    if not found:raise ValueError(f'Нет раздела {selector}; сначала выполните --list')
    parent=found[0]
    children=[s for s in sections if s.start>parent.start and s.end<=parent.end]
    # A parent selector expands to leaf sections, processed one at a time.
    candidates=children or [parent]
    leaves=[s for s in candidates if not any(x.start>s.start and x.end<=s.end for x in candidates)]
    # Preserve parent preambles too; a requirement before the first child must not vanish.
    for entry in [parent]+children:
        nested=[s for s in sections if s.start>entry.start and s.end<=entry.end]
        if nested and min(s.start for s in nested)>entry.start+1:
            leaves.append(Section(entry.id,entry.title,entry.level,entry.start,min(s.start for s in nested),entry.printed_number))
    return sorted(leaves,key=lambda s:s.start)


def extract(source,document,section,directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    assets=directory/'images';assets.mkdir(exist_ok=True)
    blocks=[];images=[];unavailable=[];raw_tables=[];lines=[f'# {section.id}. {section.title}','']
    nodes=list(document.element.body)
    for pos in range(section.start+1,section.end):
        node=nodes[pos];anchor=f'P{pos+1:04d}'
        if node.tag==qn('w:p'):
            p=Paragraph(node,document);text=p.text.strip()
            if text:
                caption=bool(re.match(r'^рисунок\b',text,re.I))
                intro=text.lower().startswith('выполнение требования подтверждается')
                blocks.append({'id':anchor,'type':'caption' if caption else 'paragraph','text':text,
                               'mandatory':bool(ACTION.search(text)) and not caption and not intro})
                lines += [f'<!-- {anchor} -->',text,'']
        elif node.tag==qn('w:tbl'):
            anchor=f'T{pos+1:04d}';table=Table(node,document);rows=[]
            for r,row in enumerate(table.rows,1):
                values=[]
                for c,cell in enumerate(row.cells,1):
                    value=cell.text.strip();values.append(value)
                    if value:blocks.append({'id':f'{anchor}.R{r}.C{c}','type':'cell','text':value,'mandatory':True})
                rows.append(values)
            if rows:
                escaped=[[v.replace('\\','\\\\').replace('|','\\|').replace('\n','<br>') for v in row] for row in rows]
                text='\n'.join(['| '+' | '.join(escaped[0])+' |','| '+' | '.join(['---']*len(escaped[0]))+' |']+
                               ['| '+' | '.join(row)+' |' for row in escaped[1:]])
                raw_tables.append({'id':anchor,'markdown':text})
                lines += [f'<!-- {anchor} -->',text,'']
        drawing=list(node.iter(qn('a:blip')))+list(node.iter('{urn:schemas-microsoft-com:vml}imagedata'))
        for number,blip in enumerate(drawing,1):
            identifier=f'I{pos+1:04d}.{number}'
            rel=blip.get(qn('r:embed')) or blip.get(qn('r:id'))
            part=document.part.related_parts.get(rel)
            if part is None:
                unavailable.append(identifier);lines += [f'**{identifier}: встроенное изображение недоступно.**',''];continue
            mime=part.content_type;extension={'image/png':'.png','image/jpeg':'.jpg','image/webp':'.webp'}.get(mime)
            if not extension:
                unavailable.append(identifier);lines += [f'**{identifier}: формат {mime} требует преобразования перед чтением.**',''];continue
            file=assets/(identifier+extension);file.write_bytes(part.blob)
            images.append({'id':identifier,'path':str(file.resolve()),'mime':mime,'sha256':hashlib.sha256(part.blob).hexdigest(),
                           'near_block':anchor})
            blocks.append({'id':identifier,'type':'image','text':f'Иллюстрация возле {anchor}','mandatory':False})
            lines += [f'![{identifier}](images/{file.name})','']
    result={'section_id':section.id,'printed_number':section.printed_number,'title':section.title,'source_sha256':hashlib.sha256(Path(source).read_bytes()).hexdigest(),
            'source_name':Path(source).name,'blocks':blocks,'images':images,'unavailable_images':unavailable,'tables':raw_tables}
    (directory/'source.md').write_text('\n'.join(lines))
    (directory/'bundle.json').write_text(__import__('json').dumps(result,ensure_ascii=False,indent=2))
    return result
