"""Versioned reference snapshot shared by UI and checks (no Tk, Excel or rules execution).

The research tables are preserved, not silently promoted to normative rules.
Only explicit executable bindings are used. No expression from a table is eval'd.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

from .errors import EditorError

SCHEMA = 'greencad.reference-bundle'
VERSION = 1
COMPILER_VERSION = '1.0'
MAX_BYTES = 20 * 1024 * 1024
DEFAULT_PATH = Path(__file__).resolve().parent.parent / 'data' / 'reference_bundle.json.gz'
GROUPS = {'conifer_tree': 'Хвойные деревья', 'conifer_shrub': 'Хвойные кустарники',
          'deciduous_tree': 'Лиственные деревья', 'deciduous_shrub': 'Лиственные кустарники',
          'vine': 'Лианы'}
SOIL_BINDINGS = {
    'R.soil.drained': 'plant.drained_soil_required',
    'R.soil.loose': 'plant.loose_soil_required',
    'R.soil.saline': 'plant.unsalted_soil_required',
    'R.soil.compacted': 'plant.uncompacted_soil_required',
    'R.water.stagnant': 'plant.stagnant_water_exclusion',
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Повторяющийся ключ JSON: '+key)
        result[key] = value
    return result


def parse_json(raw):
    if len(raw) > MAX_BYTES:
        raise EditorError('Пакет справочников превышает лимит 20 МиБ.')
    try:
        return json.loads(raw, object_pairs_hook=_unique,
                          parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise EditorError('Некорректный JSON справочника: '+str(exc)) from exc


def _index(rows, name):
    if not isinstance(rows, list) or len(rows) > 20000:
        raise EditorError(f'{name}: ожидается таблица не более 20 000 записей.')
    out = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']:
            raise EditorError(f'{name}: отсутствует строковый ID.')
        if row['id'] in out:
            raise EditorError(f'{name}: повторяется ID {row["id"]}.')
        out[row['id']] = row
    return out


def _value_type(value, dtype):
    if dtype == 'boolean': return isinstance(value, bool)
    if dtype in ('number', 'integer'):
        return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and (dtype != 'integer' or int(value) == value)
    if dtype in ('text', 'string', 'enum'): return isinstance(value, str)
    if dtype in ('enum_set', 'reference_set'): return isinstance(value, list)
    if dtype == 'interval':
        return isinstance(value, list) and len(value) == 2 and all(_value_type(v, 'number') for v in value) and value[0] <= value[1]
    return isinstance(value, (dict, list, str, int, float, bool))  # structured, not executed


def _bind_rules(registry, site, definitions):
    """Compile a deliberately bounded set of operators; unknown formulas fail closed.

    The source expression remains prose. Concrete operations are selected by the
    reviewed implementation profile, not guessed from text or cell colours.
    """
    rules = deepcopy(definitions['rules'])
    reg_rules = _index(registry['rules'], 'Проверки реестра')
    fields = _index(site['fields'], 'Поля территории')
    params = _index(registry['parameters'], 'Параметры растений')
    reg_fields = _index(registry['fields'], 'Поля реестра')
    objects = _index(site['object_types'], 'Объекты')
    for rule in rules:
        op = rule.get('operator')
        if op not in ('inside', 'outside', 'distance', 'spacing', 'territory', 'source_boolean', 'range'):
            raise EditorError(f'{rule["id"]}: обработчик {op} не поддерживается сборщиком.')
        if rule.get('kind') not in ('PROJECT', 'PROJECT_TEST', 'SOURCE_CONDITION', 'ADVISORY'):
            raise EditorError('Сборщик не активирует новые нормативные профили автоматически.')
        if rule.get('evaluation_scope') not in ('SITE_POINT', 'PLAN'):
            raise EditorError('Не определена область применения '+rule['id'])
        for rid in rule.get('registry_ids', []):
            if rid not in reg_rules: raise EditorError('Неизвестная связь с правилом '+rid)
        fid = rule.get('field_id')
        binding = {'operator': op, 'field_id': fid, 'registry_ids': rule.get('registry_ids', []),
                   'origin': 'EXPLICIT_IMPLEMENTATION_BINDING', 'normative_profile': False}
        if op == 'distance':
            if fid not in fields or fields[fid]['kind'] != 'DISTANCE':
                raise EditorError(f'{rule["id"]}: поле расстояния не найдено в словаре.')
            f = fields[fid]
            role = f['required_geometry_role']
            if role not in ('AXIS', 'EDGE', 'OUTER_CONTOUR'):
                raise EditorError('Неоднозначное основание расстояния: '+role)
            rule['object_types'] = deepcopy(f['object_type_ids'])
            rule['roles'] = [role]
            binding.update(field_registry='site', geometry_origin='SITE_DICTIONARY')
        elif fid:
            if fid not in reg_fields:
                raise EditorError(f'{rule["id"]}: поле {fid} не зарегистрировано.')
            binding['field_registry'] = 'plants'
        for oid in rule.get('object_types', []):
            if oid not in objects: raise EditorError('Незарегистрированный тип объекта '+oid)
        if op == 'source_boolean':
            source_rule = reg_rules.get(rule['id'])
            if not source_rule: raise EditorError('Не найдено исходное условие '+rule['id'])
            keys = [p for p in source_rule['parameter_ids'] if p.startswith('plant.')]
            if len(keys) != 1 or params.get(keys[0], {}).get('dtype') != 'boolean':
                raise EditorError('Для почвенной проверки требуется одна булева характеристика растения.')
            if 'site.'+rule['site_parameter'] not in source_rule['parameter_ids']:
                raise EditorError('Параметр участка не соответствует реестру: '+rule['id'])
            if type(rule.get('expected')) is not bool:
                raise EditorError('Ожидаемое значение должно быть bool.')
            # Explicit adapter polarity, not a free-form expression interpreter.
            expression = source_rule['expression'].replace(' ', '')
            expected = 'true' if rule['expected'] else 'false'
            if source_rule.get('operator_id') != 'boolean_compare' or source_rule.get('field_ids') != [fid] or expression != fid+'(x,y)=='+expected:
                raise EditorError('Изменена формула условия: нужна проверка адаптера '+rule['id'])
            rule['plant_parameter'] = keys[0]
            rule.pop('required_flag', None)
            binding.update(plant_parameter=keys[0], site_parameter=rule['site_parameter'])
        elif op == 'territory':
            binding['plant_parameter'] = 'plant.territory_mark'
        elif op == 'range':
            binding.update(site_parameter='A1', type_parameter='type_ranges', origin='USER_SCENARIO')
        if 'threshold_m' in rule:
            v = rule['threshold_m']
            if not _value_type(v, 'number') or not 0 <= v <= 1e6:
                raise EditorError('Некорректный проектный порог '+rule['id'])
            binding['threshold_origin'] = 'PROJECT_TEST'
        rule['binding'] = binding
    _index(rules, 'Исполняемые проверки')
    return rules


def compile_bundle(registry, site, definitions, *, origins=None, locations=None):
    payload = {'schema': SCHEMA, 'schema_version': VERSION, 'compiler_version': COMPILER_VERSION,
               'registry': deepcopy(registry), 'site_dictionary': deepcopy(site),
               'implementation_profile': deepcopy(definitions),
               'origins': deepcopy(origins or []), 'locations': deepcopy(locations or {})}
    payload['executable_rules'] = _bind_rules(registry, site, definitions)
    return ReferenceBundle(payload)


class ReferenceBundle:
    """A value object: callers receive copies; snapshots are shared read-only.

    Public API never exposes the mutable backing dictionaries. Original rows,
    notes and unknowns are retained; no fallbacks to botanical guesses.
    """
    def __init__(self, payload):
        try:
            if payload.get('schema') != SCHEMA or payload.get('schema_version') != VERSION or payload.get('compiler_version') != COMPILER_VERSION:
                raise EditorError('Неизвестная версия пакета справочников.')
            raw = canonical(payload)
            if len(raw) > MAX_BYTES: raise EditorError('Слишком большой пакет справочников.')
            self._data = parse_json(raw)
            self._raw = raw
            self._id = hashlib.sha256(raw).hexdigest()
            reg, site = self._data['registry'], self._data['site_dictionary']
            self._plants = _index(reg['plants'], 'Растения')
            self._parameters = _index(reg['parameters'], 'Параметры растений')
            self._site_parameters = _index(site['parameters'], 'Параметры участка')
            self._objects = _index(site['object_types'], 'Объекты')
            self._notes = _index(reg['notes'], 'Примечания')
            self._sources = _index(reg['sources'], 'Источники растений')
            self._site_sources = _index(site['sources'], 'Источники территории')
            self._fields = _index(reg['fields'], 'Поля растений')
            self._site_fields = _index(site['fields'], 'Поля территории')
            self._reg_rules = _index(reg['rules'], 'Проверки исходного реестра')
            self._rules = _index(self._data['executable_rules'], 'Исполняемые проверки')
            self._territories = _index(reg['territories'], 'Категории территории')
            for plant in self._plants.values():
                if not isinstance(plant.get('name'), str) or not plant['name'] or plant.get('source') not in self._sources:
                    raise EditorError('Некорректное описание растения '+plant['id'])
            self._values = _index(reg['values'], 'Значения характеристик')
            self._by_value = {}
            self._plant_values = {p: [] for p in self._plants}
            for row in self._values.values():
                pid, key = row['plant_id'], row['parameter_id']
                if pid not in self._plants or key not in self._parameters:
                    raise EditorError('Неизвестная ссылка в значении '+row['id'])
                if row.get('source_id') not in self._sources:
                    raise EditorError('Неизвестный источник значения '+row['id'])
                if row.get('note_id') and row['note_id'] not in self._notes:
                    raise EditorError('Неизвестное примечание '+row['note_id'])
                if not isinstance(row.get('context'), dict):
                    raise EditorError('Контекст характеристики должен быть объектом.')
                if row.get('status') not in ('KNOWN', 'UNKNOWN'):
                    raise EditorError('Неподдерживаемый статус значения '+row['id'])
                if row['status'] == 'KNOWN' and not _value_type(row['value'], self._parameters[key]['dtype']):
                    raise EditorError('Тип значения не соответствует параметру: '+row['id'])
                if key == 'plant.territory_mark':
                    if row['context'].get('territory_id') not in self._territories:
                        raise EditorError('Неизвестная категория территории у '+row['id'])
                    if row['status'] == 'KNOWN' and row['value'] not in ('PLUS', 'MINUS'):
                        raise EditorError('Неизвестная территориальная отметка.')
                index = (pid, key, canonical(row['context']))
                if index in self._by_value:
                    raise EditorError('Неоднозначные значения одной характеристики: '+row['id'])
                self._by_value[index] = row
                self._plant_values[pid].append(row)
            for obj in self._objects.values():
                for key in obj.get('parameter_ids', []):
                    if key not in self._site_parameters:
                        raise EditorError('Неизвестный параметр объекта '+key)
            for field in self._site_fields.values():
                for oid in field.get('object_type_ids', []):
                    if oid not in self._objects: raise EditorError('Неизвестный объект поля '+oid)
                for key in field.get('parameter_ids', []):
                    if key not in self._site_parameters: raise EditorError('Неизвестный параметр поля '+key)
            expected_rules = _bind_rules(reg, site, self._data['implementation_profile'])
            if expected_rules != self._data['executable_rules']:
                raise EditorError('Исполняемые связи не соответствуют исходным таблицам: пересоберите пакет.')
        except EditorError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
            raise EditorError('Неверная структура справочника: '+str(exc)) from exc

    def __deepcopy__(self, memo):
        return self

    @property
    def id(self): return self._id

    def to_bytes(self): return self._raw
    def to_dict(self): return deepcopy(self._data)
    def table(self, name, origin='plants'):
        if origin not in ('plants','site'):raise EditorError('Неизвестное пространство имён справочника.')
        root = self._data['registry' if origin == 'plants' else 'site_dictionary']
        return deepcopy(root.get(name, []))
    def origins(self): return deepcopy(self._data['origins'])
    def rules(self): return deepcopy(list(self._rules.values()))
    def rule(self, rid): return deepcopy(self._rules.get(rid))
    def objects(self): return deepcopy(self._objects)
    def territories(self): return deepcopy(list(self._territories.values()))
    def parameters(self): return deepcopy(list(self._parameters.values()))
    def plants(self): return deepcopy(list(self._plants.values()))
    def plant(self, pid): return deepcopy(self._plants.get(pid))
    def parameter(self, pid): return deepcopy(self._parameters.get(pid))

    def location(self, table, rid, origin='plants'):
        loc = deepcopy(self._data.get('locations', {}).get(origin+':'+table, {}).get(rid, {}))
        if not loc:
            loc = {'table': table, 'record_id': rid, 'source_registry': origin}
        return loc

    def value(self, pid, key, context=None):
        row = self._by_value.get((pid, key, canonical(context or {})))
        if row is None and not context and key in ('plant.name', 'plant.class', 'plant.foliage_group') and pid in self._plants:
            plant=self._plants[pid]
            value=plant['name']
            if key=='plant.class':
                value='tree' if plant['group'].endswith('_tree') else ('shrub' if plant['group'].endswith('_shrub') else 'vine' if plant['group']=='vine' else None)
            elif key=='plant.foliage_group':
                value='conifer' if plant['group'].startswith('conifer_') else ('deciduous' if plant['group'].startswith('deciduous_') else None)
            return {'plant_id':pid,'parameter_id':key,'context':{},'value':value,
                    'status':'KNOWN' if value is not None else 'UNKNOWN',
                    'source_id':plant['source'],'locator':plant['section']+'; '+plant['group']+'; №'+str(plant['number']),
                    'raw':plant.get('raw_name',plant['name']),'location':self.location('plants',pid),
                    'interpretation':'Метаданные исходной строки / группы; не измеренные размеры.'}
        if row is None:
            return {'plant_id': pid, 'parameter_id': key, 'context': context or {},
                    'value': None, 'status': 'UNKNOWN', 'reason': 'Значение не записано в реестре.'}
        out = deepcopy(row)
        out['location'] = self.location('values', row['id'])
        return out

    def values_for(self, pid):
        return [self.value(pid, r['parameter_id'], r['context']) for r in self._plant_values.get(pid, [])]

    def consumers(self, parameter_id):
        return [r['id'] for r in self._rules.values()
                if r.get('binding', {}).get('plant_parameter') == parameter_id]

    def facts(self):
        # Compatibility view for the settings dialog only; checks use typed value().
        return {'territories': self.territories(), 'sources': deepcopy(self._data['registry']['sources'])}

    def summary(self):
        return {'bundle_sha256': self.id, 'compiler_version': COMPILER_VERSION,
                'plants': len(self._plants), 'values': len(self._values),
                'parameters': len(self._parameters), 'object_types': len(self._objects),
                'site_parameters': len(self._site_parameters), 'executable_rules': len(self._rules),
                'registry_rules': len(self._reg_rules), 'normative_profile': False}


def read_bundle(path):
    path = Path(path)
    try:
        opener = gzip.open if path.suffix == '.gz' else open
        with opener(path, 'rb') as stream: raw = stream.read(MAX_BYTES+1)
        return ReferenceBundle(parse_json(raw))
    except EditorError: raise
    except (OSError, EOFError) as exc:
        raise EditorError('Не удалось прочитать пакет: '+str(exc)) from exc


def write_bundle(bundle, path):
    path = Path(path)
    if not (path.name.endswith('.json') or path.name.endswith('.json.gz')):
        raise EditorError('Пакет сохраняется только в .json или .json.gz, не поверх DXF/.gcp.')
    raw = bundle.to_bytes()
    if path.suffix == '.gz': raw = gzip.compress(raw, mtime=0)
    fd, temporary = tempfile.mkstemp(prefix='.reference_', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream: stream.write(raw)
        # Validate the exact encoded data before making the new file visible.
        check = ReferenceBundle(parse_json(gzip.decompress(raw) if path.suffix == '.gz' else raw))
        if check.id != bundle.id: raise EditorError('Проверка записанного пакета не прошла.')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@lru_cache(maxsize=1)
def default_bundle():
    return read_bundle(DEFAULT_PATH)


def fresh_default_bundle():
    # Explicit reload, never an implicit change inside an already open project.
    return read_bundle(DEFAULT_PATH)
