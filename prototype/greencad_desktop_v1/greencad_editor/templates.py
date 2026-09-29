"""Calculation templates and catalogue matching, independent of Tk and geometry.

A template is a PROJECT assumption, never a new botanical fact. Unset filters
are ignored; missing catalogue values for an active filter remain UNKNOWN.
No eval, free-form formulas or automatic conversions between unrelated scales.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, asdict
import json
from typing import Any

from .errors import EditorError
from .models import finite

TEMPLATE_VERSION = 1
CATEGORY_LABELS = {'tree': 'Деревья', 'shrub': 'Кустарники',
                   'herbaceous': 'Цветы / травянистые', 'vine': 'Лианы',
                   'generic': 'Без ограничения категории'}
ROOT_MODES = {'ANY': 'Допустима, но не обязательна',
              'FORBIDDEN': 'Не использовать', 'REQUIRED': 'Обязательна'}
OP_LABELS = {'eq': 'Равно', 'le': 'Не больше', 'ge': 'Не меньше',
             'between': 'В диапазоне', 'one_of': 'Один из',
             'contains': 'Содержит все', 'covers': 'Допускает весь диапазон'}
LIMITS = {'crown_radius_m': ('project.crown_diameter', 2.0, 'Радиус кроны / резерв в плане, м'),
          'root_radius_m': ('project.root_radius', 1.0, 'Максимальный радиус корней, м'),
          'height_max_m': ('project.total_height', 1.0, 'Максимальная полная высота, м')}
SOIL_REQUIREMENTS = {'plant.drained_soil_required': 'Требуется дренированный грунт',
                     'plant.loose_soil_required': 'Требуется рыхлый грунт',
                     'plant.unsalted_soil_required': 'Требуется незасолённый грунт',
                     'plant.uncompacted_soil_required': 'Требуется неуплотнённый грунт',
                     'plant.stagnant_water_exclusion': 'Исключить застойное увлажнение'}


def new_template() -> dict:
    return {'schema_version': TEMPLATE_VERSION, 'limits': {}, 'requirements': {},
            'minimum_distances_m': {}, 'moisture_range': None,
            'root_protection': 'ANY', 'filters': [], 'state_label': ''}


def _plain(value):
    """Enforce finite, bounded JSON data; this metadata is also exported to DXF."""
    try:
        raw = json.dumps(value, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise EditorError('Профиль должен содержать конечные JSON-значения.') from exc
    if len(raw) > 8000:
        raise EditorError('Профиль слишком велик для переносимого DXF (лимит 8 000 ASCII-символов).')


def _string(value, label):
    if not isinstance(value, str) or not value or len(value) > 160 or any(ord(c) < 32 for c in value):
        raise EditorError('Неверный идентификатор: '+label)


def _bound(value, label, maximum=1_000_000):
    number = finite(value, label)
    if not 0 <= number <= maximum:
        raise EditorError(f'{label}: требуется число от 0 до {maximum:g}.')
    return number


def operators_for(dtype):
    if dtype in ('number', 'integer'): return ('le', 'ge', 'between', 'eq')
    if dtype == 'interval': return ('between', 'covers')
    if dtype == 'boolean': return ('eq',)
    if dtype in ('text', 'enum'): return ('eq', 'one_of')
    if dtype in ('enum_set', 'reference_set'): return ('contains',)
    return ()


def available_parameters(reference):
    return [p for p in reference.parameters()
            if p.get('entity') in ('plant', 'project_state') and operators_for(p['dtype'])]


def validate_template(value, reference=None):
    """Structural validation also runs at DXF/GCP ingestion.

    With a reference snapshot, verifies IDs, units implicit in registered
    parameters, operator compatibility and context. Unknown keys fail closed.
    """
    if value is None: return None
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != TEMPLATE_VERSION:
        raise EditorError('Неизвестная версия расчётного шаблона.')
    if set(value) - set(new_template()):
        raise EditorError('Неизвестные поля расчётного шаблона.')
    result = new_template(); result.update(deepcopy(value))
    if not isinstance(result['state_label'], str) or len(result['state_label']) > 300:
        raise EditorError('Описание расчётного состояния слишком длинное.')
    if not isinstance(result['root_protection'],str) or result['root_protection'] not in ROOT_MODES:
        raise EditorError('Неизвестный режим корнезащиты.')
    for section in ('limits', 'requirements', 'minimum_distances_m'):
        if not isinstance(result[section], dict):
            raise EditorError(section+': нужен словарь значений.')
    if set(result['limits'])-set(LIMITS):
        raise EditorError('Неизвестный расчётный габарит.')
    for key, val in list(result['limits'].items()):
        if val is None: result['limits'].pop(key)
        else: result['limits'][key] = _bound(val, LIMITS[key][2], 10000)
    for key, val in list(result['requirements'].items()):
        _string(key, 'характеристика')
        if val is None: result['requirements'].pop(key); continue
        if type(val) is not bool: raise EditorError('Требование должно быть true / false / не задано.')
        if reference:
            p = reference.parameter(key)
            if not p or p.get('entity') != 'plant' or p.get('dtype') != 'boolean':
                raise EditorError('Не зарегистрировано булево требование растения '+key)
    for rid, val in list(result['minimum_distances_m'].items()):
        _string(rid, 'правило')
        if val is None: result['minimum_distances_m'].pop(rid); continue
        result['minimum_distances_m'][rid] = _bound(val, 'Отступ '+rid)
        if reference:
            r = reference.rule(rid)
            if not r or r.get('operator') not in ('distance', 'spacing'):
                raise EditorError('Минимум не связан с реализованной проверкой расстояния: '+rid)
    pair = result['moisture_range']
    if pair is not None:
        if not isinstance(pair, list) or len(pair) != 2:
            raise EditorError('Для A1 нужны обе границы или ни одной.')
        pair = [_bound(v, 'A1', 1) for v in pair]
        if pair[0] > pair[1]: raise EditorError('Нижняя граница A1 больше верхней.')
        result['moisture_range'] = pair
    if not isinstance(result['filters'], list) or len(result['filters']) > 40:
        raise EditorError('Поддерживается до 40 дополнительных критериев типа.')
    seen = set()
    for row in result['filters']:
        if not isinstance(row, dict) or set(row)-{'parameter_id', 'operator', 'value', 'context'}:
            raise EditorError('Неверная запись критерия.')
        key, op = row.get('parameter_id'), row.get('operator')
        _string(key, 'параметр фильтра')
        if not isinstance(op, str) or op not in OP_LABELS or 'value' not in row or row['value'] is None:
            raise EditorError('Не задано значение или операция критерия. Удалите ненужный критерий.')
        context = row.setdefault('context', {})
        if not isinstance(context, dict): raise EditorError('Контекст критерия должен быть объектом.')
        if context and (set(context) != {'territory_id'} or not isinstance(context['territory_id'], str)):
            raise EditorError('Поддержан явный контекст territory_id; другие контексты требуют адаптера.')
        token = (key, json.dumps(context, sort_keys=True))
        if token in seen: raise EditorError('Повторяется критерий '+key)
        seen.add(token)
        target = row['value']
        if op in ('le', 'ge'): row['value'] = finite(target, 'Граница критерия')
        elif op in ('between', 'covers'):
            if not isinstance(target, list) or len(target) != 2: raise EditorError('Нужен диапазон из двух чисел.')
            target = [finite(v, 'Граница критерия') for v in target]
            if target[0] > target[1]: raise EditorError('Границы диапазона перепутаны.')
            row['value'] = target
        elif op in ('one_of', 'contains'):
            if not isinstance(target, list) or not target or any(not isinstance(v, str) or not v for v in target):
                raise EditorError('Нужен непустой список строк.')
        elif not isinstance(target, (str, int, float, bool)):
            raise EditorError('Равенство требует одного скалярного значения.')
        if reference:
            p = reference.parameter(key)
            if not p or p.get('entity') not in ('plant', 'project_state') or op not in operators_for(p.get('dtype')):
                raise EditorError('Операция не поддерживается для параметра '+key)
            dtype = p['dtype']
            if op == 'eq':
                if dtype == 'boolean' and type(target) is not bool: raise EditorError(key+': нужно Да или Нет.')
                if dtype in ('text','enum') and not isinstance(target, str): raise EditorError(key+': нужна строка.')
                if dtype in ('number','integer'):
                    row['value'] = finite(target, key)
                    if dtype=='integer' and not row['value'].is_integer(): raise EditorError(key+': нужно целое число.')
            if key == 'plant.territory_mark':
                if context.get('territory_id') not in {t['id'] for t in reference.territories()}:
                    raise EditorError('Для территориальной отметки выберите категорию территории.')
            elif context:
                raise EditorError('Этот параметр не использует контекст territory_id.')
    _plain(result)
    return result


def validate_type_reference(spec, reference):
    validate_template(spec.template, reference)
    if spec.template is not None and (not isinstance(spec.category, str) or spec.category not in CATEGORY_LABELS):
        raise EditorError('Категория расчётного типа не поддерживается: '+str(spec.category))


def profile_radius(spec):
    return spec.template.get('limits', {}).get('crown_radius_m') if spec.template else None


def is_template_target(spec, plant_id=None, template_only=False):
    return spec.template is not None and (template_only or not (plant_id or spec.plant_id))


def _status(checks):
    states = {r['status'] for r in checks}
    return 'FAIL' if 'FAIL' in states else 'UNKNOWN' if 'UNKNOWN' in states else 'PASS'


def _compare(actual, op, target):
    if op == 'eq':
        same_kind = type(actual) is type(target) or (type(actual) in (int, float) and type(target) in (int, float))
        return 'PASS' if same_kind and actual == target else 'FAIL'
    if op in ('le','ge'):
        if type(actual) not in (int,float): return 'UNKNOWN'
        return 'PASS' if (actual <= target if op=='le' else actual >= target) else 'FAIL'
    if op == 'between':
        if type(actual) in (int,float): return 'PASS' if target[0] <= actual <= target[1] else 'FAIL'
        if isinstance(actual, list) and len(actual)==2 and all(type(v) in (int,float) for v in actual):
            if target[0] <= actual[0] <= actual[1] <= target[1]: return 'PASS'
            if actual[1] < target[0] or actual[0] > target[1]: return 'FAIL'
            return 'UNKNOWN'  # overlap does not prove the whole source range fits
        return 'UNKNOWN'
    if op == 'covers':
        if not isinstance(actual,list) or len(actual)!=2 or any(type(v) not in (int,float) for v in actual):return 'UNKNOWN'
        return 'PASS' if actual[0] <= target[0] <= target[1] <= actual[1] else 'FAIL'
    if op == 'one_of': return 'PASS' if actual in target else 'FAIL'
    if op == 'contains':
        if not isinstance(actual,list): return 'UNKNOWN'
        return 'PASS' if all(v in actual for v in target) else 'FAIL'
    return 'UNKNOWN'


def match_plant(spec, plant_id, reference):
    """Return all filter evidence. PASS is filter matching, not site approval."""
    validate_type_reference(spec, reference)
    plant = reference.plant(plant_id)
    if not plant: raise EditorError('Растение отсутствует в пакете справочников.')
    profile = spec.template or new_template()
    rows = []

    def compare(key, op, target, *, context=None, origin='FILTER', special=False):
        fact = reference.value(plant_id, key, context)
        known = fact['status'] == 'KNOWN'
        if special:
            # No requirement (False) fits a template with or without a requirement.
            status = ('PASS' if not fact['value'] or target else 'FAIL') if known and type(fact['value']) is bool else 'UNKNOWN'
        else:
            status = _compare(fact['value'], op, target) if known else 'UNKNOWN'
        rows.append({'parameter_id':key, 'label':(reference.parameter(key) or {}).get('label',key),
                     'operator':op, 'required':target, 'actual':fact.get('value'), 'status':status,
                     'reason': {'PASS':'Соответствует заданному критерию.', 'FAIL':'Не соответствует критерию.',
                                'UNKNOWN':'Недостаточно сопоставимых данных; отсутствие не заменяется нулём.'}[status],
                     'origin':origin, 'value_record':fact})

    if spec.category != 'generic':
        compare('plant.class','eq',spec.category,origin='TYPE_CATEGORY')
    for key, bound in profile['limits'].items():
        pid, factor, _ = LIMITS[key]
        compare(pid, 'le', bound*factor, origin='TYPE_LIMIT:'+key)
    for key, required in profile['requirements'].items():
        compare(key,'covered_requirement',required,origin='TYPE_REQUIREMENT',special=True)
    for row in profile['filters']:
        compare(row['parameter_id'],row['operator'],row['value'],context=row['context'])
    missing_equivalence = []
    if profile['moisture_range'] is not None:
        missing_equivalence.append('A1 — сценарная шкала типа; не приравнена к биологическому диапазону влажности.')
    if profile['root_protection'] != 'ANY':
        missing_equivalence.append('Режим корнезащиты — условие проекта; совместимость технологии с видом не установлена.')
    return {'schema':'greencad.template-match','schema_version':1,
            'type_id':spec.id,'plant_id':plant_id,'name':plant['name'], 'group':plant['group'],
            'status':_status(rows), 'checks':rows, 'unverified':missing_equivalence,
            'scope':'Только заданные критерии подбора; размещение требует отдельной проверки.',
            'reference_sha256':reference.id}


def match_catalog(spec, reference):
    validate_type_reference(spec, reference)
    result = [match_plant(spec, p['id'], reference) for p in reference.plants()]
    rank = {'PASS':0,'UNKNOWN':1,'FAIL':2}
    return sorted(result, key=lambda r:(rank[r['status']],r['name'].casefold(),r['plant_id']))


def required_inputs(spec, rules, settings):
    """Explain active-rule dependencies before any plant has been selected."""
    template = spec.template or new_template()
    result=[]
    for r in rules:
        if r['id'] not in settings.get('enabled',[]): continue
        op=r['operator']; key=None; value=None; note=''
        if op=='source_boolean':
            key=r.get('plant_parameter');value=template['requirements'].get(key)
            note='Заполните требование в расчётном профиле; пусто не означает отсутствие требования.'
        elif op=='range':
            key='moisture_range';value=template['moisture_range']
            if value is None:value=settings.get('type_ranges',{}).get(spec.id)
            note='Нужны границы типа и значение A1 участка.'
        elif op in ('distance','spacing'):
            key='minimum_distances_m.'+r['id']
            values=[settings.get('thresholds_m',{}).get(r['id'],r.get('threshold_m')),template['minimum_distances_m'].get(r['id'])]
            value=max((v for v in values if v is not None),default=None)
            note='Максимум общего проектного минимума и минимума типа; значения TEST.'
        elif op=='territory':
            key='plant.territory_mark';note='Нужен конкретный вид; у абстрактного типа нет рекомендации каталога.'
        else: note='Геометрическое условие участка; характеристики вида не требуются.'
        result.append({'rule_id':r['id'],'label':r['label'],'input':key,'value':value,
                       'status':'UNKNOWN' if key and value is None else 'READY', 'note':note})
    return result
