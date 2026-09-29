"""Sample the existing rule engine, not a second admissibility implementation.

No Tk, placement generation, file changes, screen coordinates or botanical
assumptions here. Colour belongs to the view. A cell reports ONLY its centre.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import math
from threading import Event
from typing import Callable

from shapely.geometry import Point
from shapely.ops import unary_union
from shapely.prepared import prep

from .checks import (CheckResult, PointEvaluator, ValidationRequest, aggregate,
                     catalog_digest, load_rules, make_request)
from .errors import EditorError
from .models import Placement, finite

MAP_VERSION = '1.1'
MAX_CELLS = 50_000
MAX_CHECKS = 2_000_000
STATUSES = ('OUTSIDE', 'PASS', 'FAIL', 'UNKNOWN', 'ERROR', 'NOT_CHECKED',
            'NOT_APPLICABLE', 'WARNING')
CODES = {s: i for i, s in enumerate(STATUSES)}
TOTAL = '__combined__'
SCOPE = ('Только исходная территория и выбранная посадка. Взаимодействие новых '
         'посадок не учитывается. Цвет клетки — результат в её центре, '
         'не гарантия для всей клетки и не разрешение посадить во всех точках сразу.')


@dataclass(frozen=True)
class MapOptions:
    type_id: str
    plant_id: str | None = None  # None: take the default of this project type.
    root_protection: bool = False
    step_m: float = 1.0
    template_only: bool = False

    def checked(self, types):
        if self.type_id not in types:
            raise EditorError('Проектный тип карты отсутствует в проекте.')
        if type(self.template_only) is not bool:
            raise EditorError('Режим шаблона должен быть логическим.')
        if self.template_only and types[self.type_id].template is None:
            raise EditorError('У типа пока нет расчётного шаблона. Откройте «Настроить тип».')
        if self.template_only and self.plant_id:
            raise EditorError('Режим шаблона и конкретное растение карты несовместимы.')
        step = finite(self.step_m, 'Шаг карты', positive=True)
        if not isinstance(self.root_protection, bool):
            raise EditorError('Корнезащита должна быть true/false.')
        p = Placement('__map_probe__', self.type_id, 0.0, 0.0, self.plant_id,
                      self.root_protection)
        p.validate()
        return replace(self, step_m=step)


def _digest(payload):
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode()).hexdigest()


def _signature(drawing, types, settings, options, catalog_hash):
    # Planting positions, labels, visibility and viewport are deliberately absent:
    # this map describes a fixed probe on the ORIGINAL site, not the current layout.
    return _digest({'version': MAP_VERSION, 'source': drawing.source_sha256,
                    'scale': drawing.metres_per_unit, 'mapping': drawing.layer_mapping,
                    'features': [(f.id, f.handle, f.type_id, f.geometry_role, f.properties,
                                  f.is_annotation) for f in drawing.features],
                    'types': {k: asdict(v) for k, v in types.items()},
                    'settings': settings, 'options': asdict(options),
                    'catalog': catalog_hash})


def map_fingerprint(session, options):
    return _signature(session.drawing, session.types, session.check_settings,
                      options, catalog_digest(session.reference))


def make_map_request(session, options):
    options = options.checked(session.types)
    # Existing snapshot adapter strips Canvas primitives. Do not build a spatial
    # index of thousands of placed plants for a STATIC map.
    request = make_request(session)
    request.placements = []
    request.fingerprint = _signature(request.drawing, request.types, request.settings,
                                     options, request.catalog_hash)
    return request


@dataclass(frozen=True)
class GridSpec:
    bounds: tuple[float, float, float, float]
    step_m: float
    nx: int
    ny: int

    @classmethod
    def create(cls, bounds, step_m, *, max_cells=MAX_CELLS):
        step = finite(step_m, 'Шаг карты', positive=True)
        if len(bounds) != 4:
            raise EditorError('Нужны четыре границы области карты.')
        x0, y0, x1, y1 = (finite(v, 'Граница карты') for v in bounds)
        w, h = x1-x0, y1-y0
        if w <= 0 or h <= 0 or not math.isfinite(w+h):
            raise EditorError('Нужна ограниченная область положительной площади.')
        ratios = w/step, h/step
        if not all(math.isfinite(v) for v in ratios):
            raise EditorError('Шаг слишком мал для размеров участка.')
        nx, ny = max(1, math.ceil(ratios[0])), max(1, math.ceil(ratios[1]))
        limit = min(MAX_CELLS, max_cells)
        if nx*ny > limit:
            suggestion = max(step, math.sqrt(w*h/max(1, limit))*1.1)
            raise EditorError(f'Карта содержит {nx*ny:,} клеток; лимит {limit:,}. '
                              f'Увеличьте шаг (ориентир: {suggestion:.3g} м).')
        if any(origin+step == origin for origin in (x0, y0)):
            raise EditorError('Шаг неразличим при этих координатах; укрупните сетку.')
        return cls((x0, y0, x1, y1), step, nx, ny)

    @property
    def count(self):
        return self.nx*self.ny

    def cell_bounds(self, ix, iy):
        x0, y0, x1, y1 = self.bounds
        return (x0+ix*self.step_m, y0+iy*self.step_m,
                min(x1, x0+(ix+1)*self.step_m),
                min(y1, y0+(iy+1)*self.step_m))

    def center(self, ix, iy):
        a, b, c, d = self.cell_bounds(ix, iy)
        return (a+(c-a)/2, b+(d-b)/2)

    def locate(self, x, y):
        x0, y0, x1, y1 = self.bounds
        if not x0 <= x <= x1 or not y0 <= y <= y1:
            return None
        return (min(self.nx-1, int((x-x0)/self.step_m)),
                min(self.ny-1, int((y-y0)/self.step_m)))


class MapKernel:
    """Prepared, reusable exact point query, sharing PointEvaluator with the plan.

    New rules must declare SITE_POINT explicitly. PLAN rules are listed as
    excluded. Missing/unknown scope is UNKNOWN, never a silent permission.
    """
    def __init__(self, request: ValidationRequest, options: MapOptions, *, rules=None, operators=None):
        self.options = options.checked(request.types)
        # Never share mutating input structures or use newly placed plants.
        self.request = replace(request, placements=[], types=deepcopy(request.types),
                               settings=deepcopy(request.settings))
        self.evaluator = PointEvaluator(self.request, rules=rules, operators=operators, template_only=options.template_only)
        self.rule_ids, self.excluded, self.scope_unknown = [], [], set()
        self.prototype = Placement('__map_probe__', options.type_id, 0., 0., options.plant_id, options.root_protection)
        for rid in self.request.settings['enabled']:
            r = self.evaluator.by_id.get(rid)
            if r and (r.get('evaluation_scope') == 'PLAN' or r['operator'] == 'spacing'):
                self.excluded.append({'id': rid, 'label': r['label'],
                                      'reason': 'Проверка зависит от новых посадок, не от исходной территории.'})
            else:
                self.rule_ids.append(rid)
                if r and r.get('evaluation_scope') != 'SITE_POINT':
                    self.scope_unknown.add(rid)
        # Template guards supplement only the rules which are actually mapped.
        # A plan-only selection must not turn into a green static map of guards.
        self.rule_ids = self.evaluator.effective_ids(self.prototype, self.rule_ids)
        field = self.evaluator.context.site.field(['context.green_zone'], ['AREA', 'OUTER_CONTOUR'],
                     confirmed_absent=self.request.settings['confirmed_absent'], require_area=True)
        if field.errors:
            raise EditorError('Нельзя определить область карты: '+'; '.join(field.errors))
        if not field.items:
            raise EditorError('Для карты нужна явно заданная зелёная зона положительной площади.')
        self.domain = unary_union([v.geometry for v in field.items])
        if self.domain.is_empty or not self.domain.is_valid or self.domain.area <= 0:
            raise EditorError('Зелёные зоны не образуют корректную площадь.')
        self.prepared_domain = prep(self.domain)
        self.grid = GridSpec.create(self.domain.bounds, self.options.step_m)
        if self.grid.count*max(1, len(self.rule_ids)) > MAX_CHECKS:
            raise EditorError('Слишком много проверок для сетки. Увеличьте шаг карты.')
        self.signature = request.fingerprint
        self.rule_signature = _digest(self.evaluator.rules)

    def contains(self, x, y):
        return self.prepared_domain.covers(Point(x, y))

    def evaluate(self, x, y):
        x, y = finite(x, 'X'), finite(y, 'Y')
        probe = replace(self.prototype, x=x, y=y)
        # This is the same operator dispatch, error handling and result type as
        # validate_plan. Scope-unknown rules are not executed as static rules.
        run_ids = [rid for rid in self.rule_ids if rid not in self.scope_unknown]
        outcomes = {r.rule_id: r for r in self.evaluator.evaluate(probe, run_ids)}
        for rid in self.scope_unknown:
            r = self.evaluator.by_id[rid]
            outcomes[rid] = self.evaluator.context.outcome(r, 'UNKNOWN',
                'Не задано evaluation_scope=SITE_POINT; применимость к карте не подтверждена.')
        return [outcomes[rid] for rid in self.rule_ids]

    def inspect(self, x, y):
        x, y = finite(x, 'X'), finite(y, 'Y')
        results = self.evaluate(x, y)
        return {'x': x, 'y': y, 'inside_map_domain': self.contains(x, y),
                'type_id': self.options.type_id,
                'plant_id': self.evaluator.context.effective_plant_id(self.prototype),
                'calculation_target':'TEMPLATE' if self.options.template_only else 'AUTO',
                'root_protection': self.options.root_protection,
                'status': aggregate(results, mandatory_only=True), 'checks': [asdict(r) for r in results],
                'excluded_rules': deepcopy(self.excluded), 'scope': SCOPE}


@dataclass
class AdmissibilityMap:
    kernel: MapKernel
    combined: bytearray
    layers: dict[str, bytearray]
    sampled_count: int
    created_at: str

    @property
    def grid(self):
        return self.kernel.grid

    def values(self, layer_id=TOTAL):
        if layer_id == TOTAL:
            return self.combined
        try:
            return self.layers[layer_id]
        except KeyError as exc:
            raise EditorError('Этот слой не рассчитан.') from exc

    def counts(self, layer_id=TOTAL):
        return {STATUSES[k]: v for k, v in Counter(self.values(layer_id)).items() if k != 0}

    def status_at_cell(self, ix, iy, layer_id=TOTAL):
        if not (0 <= ix < self.grid.nx and 0 <= iy < self.grid.ny):
            raise IndexError('Клетка вне сетки.')
        return STATUSES[self.values(layer_id)[iy*self.grid.nx+ix]]

    def runs(self, layer_id=TOTAL):
        """Run-length rectangles; far fewer Canvas items for uniform regions."""
        values = self.values(layer_id)
        grid = self.grid
        for iy in range(grid.ny):
            ix = 0
            while ix < grid.nx:
                value = values[iy*grid.nx+ix]
                start = ix
                ix += 1
                while ix < grid.nx and values[iy*grid.nx+ix] == value:
                    ix += 1
                if value:
                    a, b, _, d = grid.cell_bounds(start, iy)
                    _, _, c, _ = grid.cell_bounds(ix-1, iy)
                    yield (a, b, c, d, STATUSES[value])

    def to_dict(self):
        k = self.kernel
        return {'schema': 'greencad.admissibility-map', 'schema_version': 1,
                'created_at': self.created_at, 'scope': SCOPE, 'point_semantics': 'cell_center',
                'boundary_cells': 'approximate; exact clicked coordinate is re-evaluated',
                'grid': asdict(self.grid), 'row_order': 'y_ascending', 'status_codes': CODES,
                'probe': asdict(k.options),
                'resolved_plant_id': k.evaluator.context.effective_plant_id(k.prototype),
                'calculation_target': 'TEMPLATE' if k.options.template_only else 'AUTO',
                'project_type': asdict(k.request.types[k.options.type_id]),
                'source_sha256': k.request.drawing.source_sha256, 'fingerprint': k.signature,
                'catalog_sha256': k.request.catalog_hash, 'rules_sha256': k.rule_signature,
                'rules': [deepcopy(k.evaluator.by_id[r]) for r in k.rule_ids if r in k.evaluator.by_id],
                'settings': deepcopy(k.request.settings), 'excluded_rules': deepcopy(k.excluded),
                'sampled_count': self.sampled_count, 'counts': self.counts(),
                'combined': list(self.combined),
                'layers': {r: list(v) for r, v in self.layers.items()},
                'auto_planting': False, 'normative_certification': False}


def build_map(request: ValidationRequest, options: MapOptions, *, rules=None, operators=None,
              cancel: Event | None = None, progress: Callable[[int, int], None] | None = None):
    """Return None on cancellation; never publish a partial green map."""
    if cancel is not None and cancel.is_set():
        return None
    kernel = MapKernel(request, options, rules=rules, operators=operators)
    grid = kernel.grid
    layers = {rid: bytearray(grid.count) for rid in kernel.rule_ids}
    combined = bytearray(grid.count)
    sampled = 0
    for iy in range(grid.ny):
        for ix in range(grid.nx):
            if cancel is not None and cancel.is_set():
                return None
            x, y = grid.center(ix, iy)
            if not kernel.contains(x, y):
                continue
            results = kernel.evaluate(x, y)
            index = iy*grid.nx+ix
            combined[index] = CODES[aggregate(results, mandatory_only=True)]
            for result in results:
                layers[result.rule_id][index] = CODES[result.status]
            sampled += 1
        if progress:
            progress((iy+1)*grid.nx, grid.count)
    if cancel is not None and cancel.is_set():
        return None
    return AdmissibilityMap(kernel, combined, layers, sampled,
                            datetime.now(timezone.utc).isoformat())
