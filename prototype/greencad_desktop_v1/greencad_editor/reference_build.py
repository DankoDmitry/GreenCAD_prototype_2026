"""Deterministic build from the two original registries and explicit operator bindings.

The optional XLSX adapter reads only named v1 data tables, not drawing images,
formulas, arbitrary spreadsheets or standards. It uses the OOXML ZIP/XML data
format so the desktop does not need an office suite or a spreadsheet dependency.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import re
import zipfile
import xml.etree.ElementTree as ET

from .errors import EditorError
from .reference_data import compile_bundle, canonical, parse_json, MAX_BYTES, GROUPS

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / 'data' / 'reference_sources.zip'
REG = 'GreenCAD_registry_v1'
SITE = 'GreenCAD_site_dictionary_v1'
N = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
# Only these columns are an input contract; derived overview counts are ignored.
PLANT_TABLES = {
    '04_Параметры': ('parameters', ['Параметр ID','Характеристика','Объект наблюдения','Тип значения','Единица','Смысл','Наличие значений','Источники ID','Пункт / основание','Происхождение записи','Ограничения','Если данных нет'],
                       ['id','label','entity','dtype','unit','meaning','availability','source_ids','locator','origin','limits','missing_policy']),
    '03_Значения': ('values', ['Значение ID','Строка растения ID','Параметр ID','Контекст','Значение','Статус','Источник ID','Строка / пункт','Примечание ID','Исходная формулировка'],
                      ['id','plant_id','parameter_id','context','value','status','source_id','locator','note_id','raw']),
    '05_Поля': ('fields', ['Поле ID','Смысл поля','Тип','Единица','Как получить','Готовность данных','Входные параметры ID','Источники ID','Основание','Ограничения','Неполнота данных'],
                    ['id','label','dtype','unit','method','status','input_parameters','source_ids','locator','limits','missing_policy']),
    '06_Проверки': ('rules', ['Проверка ID','Проверка','Класс условия','Обработчик ID','Когда применяется','Содержание проверки','Источники ID','Пункт / таблица','Состояние','Включено','Оговорки','Нет данных','Параметры ID','Поля ID'],
                        ['id','label','kind','operator_id','applicability','expression','source_ids','locator','status','enabled','limits','missing_policy','parameter_ids','field_ids']),
    '08_Примечания': ('notes', ['Примечание ID','Текст / содержание','Источник ID','Место','Строк со ссылкой','Тип записи'],
                         ['id','raw_text','source_id','locator','uses','status']),
}
SITE_TABLES = {
    '06_Поля': ('fields', ['ID поля','Название','Тип','Типы исходных объектов','Основание измерения','Необходимые параметры','Определение / способ получения','Нет данных','Поле из реестра v1','Источники категорий'],
                   ['id','label','kind','object_type_ids','required_geometry_role','parameter_ids','method','missing_policy','legacy_field_id','_source_urls']),
}


def _xml(raw):
    if b'<!DOCTYPE' in raw or b'<!ENTITY' in raw:
        raise EditorError('DTD/ENTITY в таблице не поддерживаются.')
    return ET.fromstring(raw)


def xlsx_sheets(raw):
    """Return sparse literal cells; mark formula cells, do not trust their cache."""
    if len(raw) > MAX_BYTES: raise EditorError('Слишком большая книга.')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if len(archive.infolist()) > 1000 or sum(x.file_size for x in archive.infolist()) > 80*1024*1024:
                raise EditorError('Книга превышает лимит распаковки.')
            if len(set(archive.namelist())) != len(archive.namelist()):
                raise EditorError('Повторяющиеся записи XLSX.')
            strings=[]
            if 'xl/sharedStrings.xml' in archive.namelist():
                strings=[''.join(el.itertext()) for el in _xml(archive.read('xl/sharedStrings.xml')).findall('m:si',N)]
            relationships={x.attrib['Id']:x.attrib['Target'] for x in _xml(archive.read('xl/_rels/workbook.xml.rels'))}
            output={}
            for sheet in _xml(archive.read('xl/workbook.xml')).findall('m:sheets/m:sheet',N):
                rid=sheet.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']
                path=relationships[rid]
                path=path.lstrip('/') if path.startswith('/') else 'xl/'+path
                cells={}
                for cell in _xml(archive.read(path)).findall('m:sheetData/m:row/m:c',N):
                    coord=cell.attrib['r']; kind=cell.get('t'); node=cell.find('m:v',N)
                    if cell.find('m:f',N) is not None:
                        value={'_formula':True}
                    elif kind=='inlineStr':
                        value=''.join(t.text or '' for t in cell.findall('m:is//m:t',N))
                    elif node is None:
                        value=None
                    elif kind=='s': value=strings[int(node.text)]
                    elif kind=='b': value=node.text=='1'
                    elif kind in ('str','e'): value=node.text
                    else:
                        value=float(node.text)
                        if value.is_integer():value=int(value)
                    cells[coord]=value
                output[sheet.attrib['name']]=cells
            return output
    except EditorError:raise
    except (KeyError,ValueError,ET.ParseError,zipfile.BadZipFile,IndexError) as exc:
        raise EditorError('Некорректная XLSX-книга: '+str(exc)) from exc


def _col(i):
    value=''
    while i:
        i,r=divmod(i-1,26);value=chr(65+r)+value
    return value


def _rows(sheets,name,headers):
    if name not in sheets:raise EditorError('Не найден лист '+name)
    cells=sheets[name]
    actual=[cells.get(_col(i+1)+'5') for i in range(len(headers))]
    if actual!=headers:raise EditorError('Изменена схема заголовков '+name+' (строка 5). Нужен адаптер новой версии.')
    last=max((int(re.search(r'\d+$',k).group()) for k in cells),default=5)
    for rownum in range(6,last+1):
        vals=[cells.get(_col(i+1)+str(rownum)) for i in range(len(headers))]
        if all(v is None for v in vals):continue
        if not isinstance(vals[0],str) or not vals[0]:raise EditorError(f'{name}!A{rownum}: отсутствует ID.')
        if any(isinstance(v,dict) and v.get('_formula') for v in vals):
            raise EditorError(f'{name}, строка {rownum}: входные значения должны быть литералами, не формулами.')
        yield rownum,vals


def _decode(value,key):
    if key in ('source_ids',):return [x.strip() for x in (value or '').split(';') if x.strip()]
    if key in ('context','input_parameters','parameter_ids','field_ids','object_type_ids'):
        if isinstance(value,str) and value.strip().startswith(('[','{')):
            return json.loads(value)
        if key=='context':return {} if value is None else json.loads(value)
        return [x.strip() for x in (value or '').split(';') if x.strip()]
    return value if value is not None else ''


def apply_workbook(source,raw,name,origin,locations):
    """Overlay supported source cells by stable IDs. Preserve other raw columns."""
    source=deepcopy(source);sheets=xlsx_sheets(raw)
    sha=hashlib.sha256(raw).hexdigest()
    tables=PLANT_TABLES if origin=='plants' else SITE_TABLES
    for sheet,(table,headers,keys) in tables.items():
        old={r['id']:r for r in source[table]};new=[];locs={}
        for rownum,vals in _rows(sheets,sheet,headers):
            row=deepcopy(old.get(vals[0],{}))
            for key,val in zip(keys,vals):
                if key.startswith('_'):continue
                if key=='value':
                    param={p['id']:p for p in source['parameters']}.get(vals[2])
                    if param is None:raise EditorError('Неизвестный параметр значения '+vals[2])
                    if vals[5]=='UNKNOWN':val=None
                    elif isinstance(val,str) and param['dtype'] in ('reference_set','enum_set','interval','structured'):
                        val=json.loads(val)
                    row[key]=val
                else:row[key]=_decode(val,key)
            new.append(row)
            locs[row['id']]={'file':name,'file_sha256':sha,'sheet':sheet,'row':rownum,
                            'range':f'A{rownum}:{_col(len(keys))}{rownum}',
                            'cells':{key:_col(i+1)+str(rownum) for i,key in enumerate(keys) if not key.startswith('_')},
                            'authority':'XLSX_LITERAL'}
        source[table]=new;locations[origin+':'+table]=locs
    if origin=='plants':
        headers=['Строка ID','Название из источника','Ассортимент','Группа','Примечания','Двор','Дошкольные','Школы / спорт','Медицинские\nучреждения','Дороги*','Площади','Парки*','Промзоны*','Точность наименования','Источник ID','Место в источнике','Исходная запись с примечаниями']
        old={r['id']:r for r in source['plants']};out=[];locs={}
        groups={v:k for k,v in GROUPS.items()}
        for num,v in _rows(sheets,'02_Растения',headers):
            if v[0] not in old:raise EditorError('Новая строка растения требует заполненного JSON-паспорта: '+v[0])
            row=deepcopy(old[v[0]])
            row.update(name=v[1],source=v[14],raw_name=v[16])
            if v[3] in groups:row['group']=groups[v[3]]
            out.append(row)
            locs[row['id']]={'file':name,'file_sha256':sha,'sheet':'02_Растения','row':num,
                             'range':f'A{num}:Q{num}','authority':'XLSX_LITERAL'}
        source['plants']=out;locations['plants:plants']=locs
    else:
        headers=['ID типа','Тематическая группа','Название объекта / категории','Документ','Пункт / строка таблицы','Происхождение','Роль в модели','Ограничение интерпретации','URL источника']
        old={r['id']:r for r in source['object_types']};out=[];locs={}
        for num,v in _rows(sheets,'02_Объекты',headers):
            if v[0] not in old:raise EditorError('Новый объект требует JSON-профиля: '+v[0])
            row=deepcopy(old[v[0]]);row.update(label=v[2],source_locator=v[4],role=v[6],notes=v[7] or '',source_url=v[8] or '')
            out.append(row);locs[row['id']]={'file':name,'file_sha256':sha,'sheet':'02_Объекты','row':num,'range':f'A{num}:I{num}','authority':'XLSX_LITERAL'}
        source['object_types']=out;locations['site:object_types']=locs
        # Site parameter definitions are additional metadata, not default values.
        headers=['ID параметра','Название','Тип значения','Единица','Смысл','Диапазон / ограничения','Источник смысла','Пункт / связь','Происхождение','Ссылка на незаданный параметр','Роль в 2D','URL']
        old={r['id']:r for r in source['parameters']};out=[];locs={}
        for num,v in _rows(sheets,'04_Параметры',headers):
            if v[0] not in old:raise EditorError('Новый параметр объекта требует JSON-описания: '+v[0])
            row=deepcopy(old[v[0]]);row.update(label=v[1],dtype=v[2],unit=v[3],meaning=v[4],domain=v[5],source_locator=v[7],parameter_ref=v[9])
            out.append(row);locs[row['id']]={'file':name,'file_sha256':sha,'sheet':'04_Параметры','row':num,'range':f'A{num}:L{num}','authority':'XLSX_LITERAL'}
        source['parameters']=out;locations['site:parameters']=locs
    return source


def source_inputs(path=SOURCES):
    with zipfile.ZipFile(path) as z:
        if len(z.infolist())>10 or sum(x.file_size for x in z.infolist())>12*1024*1024:
            raise EditorError('Исходный комплект слишком велик.')
        return {n:z.read(n) for n in z.namelist()}


def build_from_sources(*, sources_path=SOURCES, registry_path=None, site_path=None,
                       registry_xlsx=None, site_xlsx=None, use_workbooks=True, profile_path=None):
    files=source_inputs(sources_path)
    origins=[];locations={}
    def read(stem,override):
        raw=Path(override).read_bytes() if override else files[stem+'.json']
        name=Path(override).name if override else stem+'.json'
        origins.append({'file':name,'sha256':hashlib.sha256(raw).hexdigest(),'role':'SOURCE_JSON'})
        data=parse_json(raw)
        for table,rows in data.items():
            if isinstance(rows,list):
                locs={r['id']:{'file':name,'file_sha256':hashlib.sha256(raw).hexdigest(),
                              'pointer':f'/{table}/{i}','authority':'JSON_RECORD'}
                      for i,r in enumerate(rows) if isinstance(r,dict) and isinstance(r.get('id'),str)}
                if locs:locations[('plants' if stem==REG else 'site')+':'+table]=locs
        return data
    reg=read(REG,registry_path);site=read(SITE,site_path)
    # A custom JSON input is authoritative unless an XLSX overlay is explicitly supplied.
    for stem,override,json_override,origin in ((REG,registry_xlsx,registry_path,'plants'),(SITE,site_xlsx,site_path,'site')):
        if override or (use_workbooks and not json_override):
            raw=Path(override).read_bytes() if override else files[stem+'.xlsx']
            name=Path(override).name if override else stem+'.xlsx'
            origins.append({'file':name,'sha256':hashlib.sha256(raw).hexdigest(),'role':'NAMED_XLSX_TABLES'})
            if origin=='plants':reg=apply_workbook(reg,raw,name,origin,locations)
            else:site=apply_workbook(site,raw,name,origin,locations)
    profile=Path(profile_path or ROOT/'data'/'check_rules.json')
    raw=profile.read_bytes();definitions=parse_json(raw)
    # The implementation profile is text under Git: CRLF/LF must not change a build.
    origins.append({'file':profile.name,'sha256':hashlib.sha256(canonical(definitions)).hexdigest(),
                    'hash_basis':'CANONICAL_JSON', 'role':'IMPLEMENTATION_PROFILE_TEST_NOT_NORM'})
    return compile_bundle(reg,site,definitions,origins=origins,locations=locations)
