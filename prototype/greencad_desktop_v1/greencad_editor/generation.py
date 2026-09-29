"""Deterministic, bounded, additive placement of calculation templates.

No Tk, raster classification, independent distances, or botanical assumptions.
A private PointEvaluator provides ALL site/pair predicates; a fresh validate_plan
is the final gate. Preview and apply are separate transactions. A finite greedy
pass is neither a continuous optimum nor a proof of infeasibility.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import random
import time
from typing import Callable
from uuid import NAMESPACE_URL, uuid5

from shapely.geometry import Point
from shapely.prepared import prep

from .admissibility import GridSpec
from .checks import (CheckResult, PointEvaluator, ValidationRequest, ValidationCancelled,
                     aggregate, fingerprint, fingerprint_state, load_rules,
                     validate_plan, validate_settings)
from .errors import EditorError
from .models import Placement, ProjectType, finite, required_id
from .templates import validate_type_reference

GENERATOR_VERSION = '1.1'
LAYOUT_MODES = ('grid', 'natural')
MAX_SEED = 2**32 - 1
LAYOUT_LABELS = {'grid': 'Рядами (как раньше)', 'natural': 'Свободная (без рядов)'}
MAX_GRID_POINTS = 50_000
MAX_TARGETS = 20
MAX_NEW_PLACEMENTS = 2_000
MAX_TOTAL_PLACEMENTS = 20_000
MAX_TRIALS = 200_000
MAX_RULE_EVALUATIONS = 1_000_000
REQUIRED_RULES = ('R.domain', 'project.excluded', 'R.spacing')
ZONE_RULE_ID = 'generation.selected_zone'
SCOPE = ('Предварительная расстановка по конечному набору пробных позиций; не глобальный оптимум. '
         'Проверены только эффективные правила и заданные проектные профили. '
         'Численные TEST-пороги не являются нормативным заключением. '
         'Каталог не заполняется догадками; анонимный тип не подтверждает пригодность вида.')


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class GenerationTarget:
    type_id: str
    max_new: int = 20


@dataclass(frozen=True)
class GenerationOptions:
    zone_id: str
    targets: tuple[GenerationTarget, ...]
    step_m: float = 1.0
    # The API default preserves existing callers/JSON jobs. New UI jobs use natural.
    layout: str = 'grid'
    seed: int = 1

    def checked(self, types: dict[str, ProjectType], existing_count: int = 0):
        required_id(self.zone_id, 'ID зелёной зоны')
        if self.layout not in LAYOUT_MODES:
            raise EditorError('Неизвестный способ расстановки: grid или natural.')
        if type(self.seed) is not int or not 0 <= self.seed <= MAX_SEED:
            raise EditorError(f'Номер варианта — целое от 0 до {MAX_SEED}.')
        if not isinstance(self.targets, (tuple, list)) or not 1 <= len(self.targets) <= MAX_TARGETS:
            raise EditorError(f'Выберите от 1 до {MAX_TARGETS} проектных типов.')
        ids, total = set(), 0
        for target in self.targets:
            if not isinstance(target, GenerationTarget) or target.type_id not in types:
                raise EditorError('Задание ссылается на неизвестный проектный тип.')
            if target.type_id in ids:
                raise EditorError('Один тип не должен повторяться в очереди.')
            ids.add(target.type_id)
            if types[target.type_id].template is None:
                raise EditorError(f'{target.type_id}: сначала включите самостоятельный расчётный шаблон в карточке типа.')
            if type(target.max_new) is not int or not 1 <= target.max_new <= MAX_NEW_PLACEMENTS:
                raise EditorError(f'{target.type_id}: максимум новых экземпляров — целое от 1 до {MAX_NEW_PLACEMENTS}.')
            total += target.max_new
        if total > MAX_NEW_PLACEMENTS or existing_count + total > MAX_TOTAL_PLACEMENTS:
            raise EditorError(f'В одном задании не более {MAX_NEW_PLACEMENTS} новых посадок; '
                              f'всего в проекте не более {MAX_TOTAL_PLACEMENTS}. Уменьшите лимиты.')
        return replace(self, targets=tuple(self.targets), step_m=finite(self.step_m, 'Шаг поиска', positive=True))

    @classmethod
    def from_dict(cls, data):
        required = {'zone_id', 'targets', 'step_m'}
        if (not isinstance(data, dict) or not required <= set(data)
                or set(data) - required - {'layout', 'seed'}):
            raise EditorError('Ожидается задание с zone_id, targets, step_m; дополнительно layout и seed.')
        if not isinstance(data['targets'], list): raise EditorError('targets должен быть списком.')
        targets = []
        for item in data['targets']:
            if not isinstance(item, dict) or set(item) != {'type_id', 'max_new'}:
                raise EditorError('Каждый тип требует type_id и max_new.')
            targets.append(GenerationTarget(**item))
        return cls(data['zone_id'], tuple(targets), data['step_m'],
                   data.get('layout', 'grid'), data.get('seed', 1))



def search_candidates(grid, prepared_domain, options, notify, stop):
    """One probe per search cell; natural probes are jittered BEFORE validation.

    Stable row-major random draws followed by sorting random priorities remove
    both aligned rows and the corner-first sweep. No geometry is moved after
    acceptance. A local RNG does not affect any other application component.
    The step controls finite sampling, not a distance or a packing guarantee.
    """
    rng = random.Random(options.seed) if options.layout == 'natural' else None
    candidates = []
    for iy in range(grid.ny):
        notify('Подготовка пробных позиций', iy, grid.ny)
        for ix in range(grid.nx):
            index = iy * grid.nx + ix
            if index % 256 == 0:
                stop()
            if rng is None:
                x, y = grid.center(ix, iy)
                priority = index
            else:
                a, b, c, d = grid.cell_bounds(ix, iy)
                x = a + (c - a) * rng.random()
                y = b + (d - b) * rng.random()
                # Draw even for outside probes: holes do not change the random
                # sequence assigned to the other cells of the same grid.
                priority = rng.random()
            if prepared_domain.covers(Point(x, y)):
                candidates.append((priority, index, x, y))
    if rng is not None:
        candidates.sort(key=lambda row: (row[0], row[1]))
    stop()
    return [(index, x, y) for _, index, x, y in candidates]


def zone_choices(drawing):
    """Fast semantic list for the UI; geometric validity is checked in the worker."""
    return [{'id': f.id, 'label': str(f.properties.get('name') or f.id), 'layer': f.layer}
            for f in drawing.features if f.type_id == 'context.green_zone' and not f.is_annotation]


def _state(request, settings=None, placements=None):
    return {'types': [asdict(t) for t in request.types.values()],
            'placements': [asdict(p) for p in (request.placements if placements is None else placements)],
            'layer_mapping': deepcopy(request.drawing.layer_mapping),
            'check_settings': deepcopy(request.settings if settings is None else settings),
            'reference_id': request.reference.id}



def mandatory_report_status(report):
    if not report['placements']: return 'EMPTY'
    checks = [CheckResult(**v) for row in report['placements'] for v in row['checks']]
    return aggregate(checks, mandatory_only=True)


def _effective_settings(request, rules):
    settings = validate_settings(request.settings, request.reference)
    by_id = {r['id']: r for r in rules}
    added = [rid for rid in REQUIRED_RULES if rid not in settings['enabled']]
    settings['enabled'] = list(dict.fromkeys(settings['enabled'] + list(REQUIRED_RULES)))
    required_operators = dict(zip(REQUIRED_RULES, ('inside', 'outside', 'spacing')))
    for rid in settings['enabled']:
        rule = by_id.get(rid)
        if rule is None:
            raise EditorError(f'Генерация не начата: неизвестная проверка {rid}.')
        scope = rule.get('evaluation_scope')
        if scope not in ('SITE_POINT', 'PLAN'):
            raise EditorError(f'{rid}: неизвестна область действия проверки; нельзя пропустить условие.')
        if scope == 'PLAN' and rule.get('operator') != 'spacing':
            raise EditorError(f'{rid}: первый генератор поддерживает только попарные PLAN-условия расстояния. '
                              'Новая глобальная проверка требует адаптера генерации.')
        expected_scope = 'PLAN' if rid == 'R.spacing' else 'SITE_POINT'
        if rid in required_operators and (rule.get('operator') != required_operators[rid]
                                         or rule.get('kind') == 'ADVISORY' or scope != expected_scope):
            raise EditorError(f'{rid}: обязательная защитная проверка имеет несовместимое определение.')
    return settings, added


@dataclass
class GenerationResult:
    options: GenerationOptions
    base_fingerprint: str
    placements: tuple[Placement, ...]
    proposed_state: dict
    validation: dict
    report: dict
    integrity: str = ''

    @property
    def can_apply(self):
        return self.report['status'] in ('READY', 'PARTIAL') and bool(self.placements)

    def seal(self):
        self.integrity = self._checksum()
        return self

    def _checksum(self):
        return _digest({'options': asdict(self.options), 'base': self.base_fingerprint,
                        'placements': [asdict(p) for p in self.placements],
                        'state': self.proposed_state, 'validation': self.validation, 'report': self.report})

    def export(self):
        return {**deepcopy(self.report), 'options': asdict(self.options),
                'base_fingerprint': self.base_fingerprint,
                'proposed_fingerprint': self.validation.get('fingerprint'),
                'placements': [asdict(p) for p in self.placements],
                'validation': deepcopy(self.validation)}


def generate_plan(request: ValidationRequest, options: GenerationOptions, *, cancel=None,
                  progress: Callable[[str, int, int], None] | None = None):
    """Return a complete preview or None on cancellation. Never edits input.

    Positions are grid centres or seeded jittered probes in shuffled order.
    All active conditions are evaluated; advisory results are retained but do not veto.
    Final verification is fresh and includes the entire unchanged manual plan.
    """
    started = time.perf_counter()
    def stop():
        if cancel is not None and cancel.is_set(): raise ValidationCancelled('Расстановка отменена.')
    def notify(stage, done, total):
        stop()
        if progress is not None: progress(stage, done, total)
        stop()
    options = options.checked(request.types, len(request.placements))
    if request.reference is None: raise EditorError('Для генерации нужен явный снимок справочника.')
    if fingerprint_state(request.drawing, _state(request), request.reference) != request.fingerprint:
        raise EditorError('Исходный снимок изменён; заново подготовьте задание генерации.')
    for t in request.types.values(): validate_type_reference(t, request.reference)
    rules = load_rules(request.reference)
    settings, added_rules = _effective_settings(request, rules)
    work = replace(request, placements=deepcopy(request.placements), types=deepcopy(request.types), settings=settings)
    original_ids = {p.id for p in work.placements}
    if len(original_ids) != len(work.placements): raise EditorError('Повтор ID исходной посадки.')
    if any(p.type_id not in work.types for p in work.placements): raise EditorError('Неизвестный тип исходной посадки.')
    for p in work.placements: p.validate()
    result_report = {'schema': 'greencad.generation-report', 'schema_version': 1,
                     'generator_version': GENERATOR_VERSION, 'status': 'PREPARING', 'scope': SCOPE,
                     'created_at': datetime.now(timezone.utc).isoformat(),
                     'source': {'name': request.drawing.source_name, 'sha256': request.drawing.source_sha256},
                     'reference_sha256': request.reference.id, 'added_rules': added_rules,
                     'effective_settings': deepcopy(settings), 'statistics': [], 'rejections': [],
                     'algorithm': ('sequential_greedy_yx_cell_centres' if options.layout == 'grid'
                                   else 'sequential_greedy_stratified_random_v1'),
                     'sampling': {'layout': options.layout,
                                  'seed': options.seed if options.layout == 'natural' else None,
                                  'jitter_before_validation': options.layout == 'natural',
                                  'sequence': 'Random.random / three draws per cell / priority sort v1'},
                     'rules': deepcopy(rules),
                     'manual_count': len(original_ids), 'geometry_note': 'XY in metres, original calculation geometry, not pixels'}
    try:
        notify('Подготовка участка', 0, 1)
        evaluator = PointEvaluator(work, rules=rules)
        # Restricted domain is another invocation of the SAME inside operator.
        zone_rule = {'id': ZONE_RULE_ID, 'label': 'Резерв внутри выбранной зелёной зоны',
                     'kind': 'PROJECT', 'operator': 'inside', 'evaluation_scope': 'SITE_POINT',
                     'object_types': ['context.green_zone'], 'roles': ['AREA', 'OUTER_CONTOUR'],
                     'feature_ids': [options.zone_id], 'description': 'Граница выбранного задания, включая отверстия.',
                     'provenance': [{'source_id': 'PROJECT', 'locator': 'Задание генерации / zone_id'}]}
        evaluator.rules.append(zone_rule)
        evaluator.by_id[ZONE_RULE_ID] = zone_rule
        field = evaluator.context.field(zone_rule, area=True)
        field.require()
        if len(field.items) != 1: raise EditorError('Нужна одна однозначно идентифицированная зелёная зона.')
        domain = field.items[0].geometry
        prepared = prep(domain)
        grid = GridSpec.create(domain.bounds, options.step_m, max_cells=MAX_GRID_POINTS)
        effective_count = len(settings['enabled']) + 3
        if grid.count * len(options.targets) > MAX_TRIALS or grid.count * len(options.targets) * effective_count > MAX_RULE_EVALUATIONS:
            raise EditorError('Слишком много пробных проверок. Увеличьте шаг или сократите число типов.')
        if (len(work.placements) + sum(t.max_new for t in options.targets)) * effective_count > MAX_RULE_EVALUATIONS:
            raise EditorError('Полная проверка плана превышает лимит операций. Уменьшите задание.')
        # Never stack zero-size anonymous positions or pretend symbol sizes are real.
        spacing_rule = evaluator.by_id['R.spacing']
        for t in options.targets:
            p = Placement('__probe__', t.type_id, 0, 0)
            if max(evaluator.context.threshold(spacing_rule, p), 2 * evaluator.context.radius(p)) <= 0:
                raise EditorError(f'{t.type_id}: задайте положительный резерв или минимум межпосадочного расстояния.')
        result_report['grid'] = asdict(grid)
        result_report['rules'].append(deepcopy(zone_rule))
        # Existing placements are not deleted or repaired. Their failures stop apply.
        base_state = _state(work)
        base_req = replace(work, fingerprint=fingerprint_state(work.drawing, base_state, work.reference))
        baseline = validate_plan(base_req, rules=rules, cancel=cancel,
                                 progress=lambda n, count: notify('Проверка ручного плана', n, count))
        baseline['mandatory_status'] = mandatory_report_status(baseline)
        result_report['baseline_status'] = baseline['mandatory_status']
        if baseline['mandatory_status'] not in ('PASS', 'EMPTY'):
            result_report.update(status='BASELINE_BLOCKED', elapsed_seconds=time.perf_counter()-started,
                                 message='В исходном плане есть нарушения или недостаток обязательных данных. '
                                         'Существующие посадки не изменены; сначала проверьте их.', new_count=0)
            return GenerationResult(options, request.fingerprint, (), base_state, baseline, result_report).seal()
        # The same predicates validate grid and free probes; no post-check jitter.
        candidates = search_candidates(grid, prepared, options, notify, stop)
        result_report['candidate_count'] = len(candidates)
        job_id = _digest({'base': request.fingerprint, 'options': asdict(options),
                          'settings': settings, 'version': GENERATOR_VERSION})
        result_report['job_id'] = job_id
        generated = []
        done, total = 0, len(candidates) * len(options.targets)
        for target in options.targets:
            t = work.types[target.type_id]
            root_flag = t.template['root_protection'] == 'REQUIRED'
            stat = {'type_id': target.type_id, 'name': t.name, 'max_new': target.max_new,
                    'placed': 0, 'examined': 0, 'outcomes': {}, 'rule_rejections': {}, 'stop_reason': ''}
            outcome_count, rule_count = Counter(), Counter()
            for index, x, y in candidates:
                if stat['placed'] >= target.max_new: break
                if done % 32 == 0: notify(f'Расстановка {target.type_id}', done, total)
                stop()
                pid = uuid5(NAMESPACE_URL, f'greencad:{job_id}:{target.type_id}:{index}').hex
                if pid in original_ids: raise EditorError('Коллизия ID генерации; исходные посадки не изменены.')
                p = Placement(pid, target.type_id, x, y, root_protection=root_flag,
                              label=f'{target.type_id} · авто {stat["placed"]+1}',
                              properties={'generation': {'job_id': job_id, 'version': GENERATOR_VERSION,
                                           'zone_id': options.zone_id, 'grid_index': index, 'step_m': options.step_m,
                                           'layout': options.layout, 'seed': options.seed if options.layout == 'natural' else None,
                                           'order_and_caps': [[target.type_id, target.max_new] for target in options.targets]}})
                results = evaluator.evaluate(p, settings['enabled'] + [ZONE_RULE_ID])
                mandatory = aggregate(results, mandatory_only=True)
                if any(r.status == 'ERROR' for r in results):
                    raise EditorError('Ошибка исполнителя при генерации: ' + '; '.join(r.reason for r in results if r.status == 'ERROR'))
                stat['examined'] += 1
                done += 1
                outcome_count[mandatory] += 1
                if mandatory == 'PASS':
                    generated.append(deepcopy(p))
                    evaluator.append_placement(p)
                    stat['placed'] += 1
                else:
                    rejected_checks = [r for r in results if r.status not in ('PASS', 'NOT_APPLICABLE')]
                    for r in rejected_checks: rule_count[r.rule_id] += 1
                    # All visited rejections, not just a sample. Repeated provenance
                    # is in report.rules; accepted decisions are in final validation.
                    result_report['rejections'].append({'type_id': t.id, 'grid_index': index, 'x': x, 'y': y,
                        'status': mandatory, 'checks': [{'rule_id': r.rule_id, 'status': r.status,
                           'reason': r.reason, 'actual': r.actual, 'required': r.required,
                           'unit': r.unit, 'object_ids': r.object_ids} for r in rejected_checks]})
            stat['outcomes'] = dict(outcome_count)
            stat['rule_rejections'] = dict(rule_count)
            stat['stop_reason'] = 'QUOTA_REACHED' if stat['placed'] == target.max_new else 'CANDIDATES_EXHAUSTED'
            result_report['statistics'].append(stat)
            # Account for unvisited nodes after a quota, without calling them rejects.
            done += len(candidates) - stat['examined']
            notify(f'{target.type_id}: добавлено {stat["placed"]}', done, total)
        stop()
        proposed = _state(work)
        proposed_fp = fingerprint_state(work.drawing, proposed, work.reference)
        final_request = replace(work, fingerprint=proposed_fp)
        # Fresh context and spatial index, separate from incremental accepted-set cache.
        final = validate_plan(final_request, rules=rules, cancel=cancel,
            progress=lambda n, count: notify('Полная проверка результата', n, count))
        zone_evaluator = PointEvaluator(final_request, rules=rules + [zone_rule])
        by_id = {row['id']: row for row in final['placements']}
        for p in generated:
            stop()
            extra = next(r for r in zone_evaluator.evaluate(p, [ZONE_RULE_ID]) if r.rule_id == ZONE_RULE_ID)
            row = by_id[p.id]
            row['checks'].append(asdict(extra))
            row['status'] = aggregate([CheckResult(**r) for r in row['checks']])
        final['rules'].append(deepcopy(zone_rule))
        final['automatic_rules'] = list(dict.fromkeys(final['automatic_rules'] + ([ZONE_RULE_ID] if generated else [])))
        final['counts'] = dict(Counter(r['status'] for r in final['placements']))
        final['status'] = aggregate([CheckResult(**c) for r in final['placements'] for c in r['checks']]) if final['placements'] else 'EMPTY'
        final['mandatory_status'] = mandatory_report_status(final)
        complete = all(s['placed'] == s['max_new'] for s in result_report['statistics'])
        state = 'READY' if complete else ('PARTIAL' if generated else 'EMPTY')
        if final['mandatory_status'] not in ('PASS', 'EMPTY'): state = 'VALIDATION_BLOCKED'
        result_report.update(status=state, new_count=len(generated),
                             mandatory_status=final['mandatory_status'], elapsed_seconds=time.perf_counter()-started,
                             message=('Квоты достигнуты.' if complete else
                                      'В данном проходе найдено меньше заданного лимита; это не доказательство отсутствия других композиций.'))
        notify('Готово', total, total)
        return GenerationResult(options, request.fingerprint, tuple(generated), proposed, final, result_report).seal()
    except ValidationCancelled:
        return None


def apply_generation(session, result: GenerationResult):
    """One undoable commit; refuse stale, failed or altered previews."""
    if not isinstance(result, GenerationResult) or not result.can_apply:
        raise EditorError('Этот результат нельзя применить: нет подтверждённых новых посадок.')
    if result.integrity != result._checksum():
        raise EditorError('Предварительный результат изменён. Пересчитайте расстановку.')
    if fingerprint(session) != result.base_fingerprint:
        raise EditorError('Предварительная расстановка устарела. Пересчитайте по текущему проекту.')
    if mandatory_report_status(result.validation) != 'PASS':
        raise EditorError('Итоговый план не прошёл обязательные проверки.')
    expected = fingerprint_state(session.drawing, result.proposed_state, session.reference)
    if expected != result.validation['fingerprint']:
        raise EditorError('Снимок отчёта не соответствует предлагаемому плану.')
    # restore validates the WHOLE state before assignment; rollback on an unforeseen failure.
    previous, selected = session.state(), session.selected_id
    try:
        session.restore(deepcopy(result.proposed_state))
    except Exception:
        session.restore(previous)
        session.selected_id = selected
        raise
    session.selected_id = result.placements[0].id
    session._commit()
    session.validation_report = deepcopy(result.validation)
    return len(result.placements)
