"""Calculation geometry from ORIGINAL DXF, never from Canvas preview vertices.

v0.2 deliberately supports straight LINE/LWPOLYLINE/POLYLINE and straight
HATCH boundaries. Unsupported/invalid relevant geometry is UNKNOWN, not omitted.
Coordinates are horizontal WCS metres; Z is deliberately ignored. No OCR.
"""
from __future__ import annotations
from dataclasses import dataclass
import math
from shapely.geometry import Point, LineString, Polygon
from shapely.strtree import STRtree
from .dxf_io import read_document
from .models import Drawing, Feature


class MissingGeometry(ValueError):
    pass


@dataclass
class GeometryItem:
    feature: Feature
    geometry: object


def _polygon(points):
    poly = Polygon(points)
    if poly.is_empty or not poly.is_valid or poly.area <= 0:
        raise MissingGeometry('Контур должен быть замкнутым простым многоугольником с положительной площадью.')
    return poly


def _geometry(entity, feature, scale):
    """Only exact straight geometry in this implementation (no silent flattening)."""
    kind, role = entity.dxftype(), feature.geometry_role
    area = role in {'AREA', 'OUTER_CONTOUR'}
    def xy(v):
        x, y = float(v[0])*scale, float(v[1])*scale
        if not math.isfinite(x) or not math.isfinite(y):
            raise MissingGeometry('Неконечные координаты.')
        return x, y
    if kind == 'LINE':
        if area or role == 'CENTER':
            raise MissingGeometry('LINE не задаёт площадь или единственный центр.')
        geom = LineString([xy(entity.dxf.start), xy(entity.dxf.end)])
    elif kind == 'POINT':
        if role != 'CENTER':
            raise MissingGeometry('POINT поддерживается только с ролью CENTER.')
        geom = Point(xy(entity.dxf.location))
    elif kind in {'LWPOLYLINE', 'POLYLINE'}:
        if kind == 'LWPOLYLINE':
            if any(float(p[4]) != 0 for p in entity.get_points('xyseb')):
                raise MissingGeometry('Дуги bulge ещё не поддерживаются расчётным адаптером.')
            if entity.dxf.get('const_width', 0) or any(p[2] or p[3] for p in entity.get_points('xyseb')):
                if role != 'AXIS':
                    raise MissingGeometry('Ширина полилинии не является явно заданной границей.')
            points = [xy(v) for v in entity.vertices_in_wcs()]
            closed = entity.closed
        else:
            if not (entity.is_2d_polyline or entity.is_3d_polyline):
                raise MissingGeometry('Сетки POLYLINE не поддерживаются.')
            if entity.dxf.flags & (2 | 4) or any(v.dxf.get('bulge', 0) for v in entity.vertices):
                raise MissingGeometry('Сглаженная POLYLINE/bulge не поддерживается.')
            points = [xy(v) for v in entity.points_in_wcs()]
            closed = entity.is_closed
        if len(points) < 2:
            raise MissingGeometry('У полилинии недостаточно точек.')
        if role == 'CENTER':
            raise MissingGeometry('Полилиния не задаёт единственный центр.')
        if area:
            if not closed:
                raise MissingGeometry('Площадной объект передан незамкнутой полилинией.')
            geom = _polygon(points)
        else:
            if closed and points[0] != points[-1]:
                points.append(points[0])
            geom = LineString(points)
    elif kind == 'CIRCLE' and role == 'CENTER':
        # A dendro symbol with explicit CENTER: use only its centre, not symbol radius.
        geom = Point(xy(entity.ocs().to_wcs(entity.dxf.center)))
    elif kind == 'HATCH' and area:
        if entity.dxf.get('hatch_style', 0) != 0:
            raise MissingGeometry('Поддерживается HATCH с обычным вложением границ (hatch_style=0).')
        rings = []
        for path in entity.paths:
            if not hasattr(path, 'vertices') or not path.is_closed:
                raise MissingGeometry('Для HATCH нужны замкнутые полилинейные границы без дуг.')
            if any(len(v) > 2 and float(v[2]) != 0 for v in path.vertices):
                raise MissingGeometry('Дуговая граница HATCH ещё не поддерживается.')
            z = entity.dxf.elevation.z
            points = [xy(entity.ocs().to_wcs((v[0], v[1], z))) for v in path.vertices]
            rings.append(_polygon(points))
        if not rings:
            raise MissingGeometry('HATCH без границ.')
        for i, ring in enumerate(rings):
            for other in rings[i+1:]:
                if ring.boundary.intersects(other.boundary):
                    raise MissingGeometry('Пересекающиеся или касающиеся кольца HATCH неоднозначны.')
        # Even/odd nesting preserves holes and nested islands, independent of winding.
        geom = rings[0]
        for ring in rings[1:]:
            geom = geom.symmetric_difference(ring)
    else:
        raise MissingGeometry(f'{kind}/{role}: расчётная геометрия ещё не поддерживается; вид на экране не используется вместо неё.')
    if geom.is_empty or not geom.is_valid:
        raise MissingGeometry('Пустая или некорректная геометрия.')
    if geom.geom_type == 'LineString' and geom.length == 0:
        raise MissingGeometry('Линия нулевой длины.')
    if not all(math.isfinite(v) for v in geom.bounds):
        raise MissingGeometry('Неконечные границы геометрии.')
    return geom


class GeometryField:
    def __init__(self, items, errors, absent=False):
        self.items, self.errors, self.absent = items, errors, absent
        self.tree = STRtree([i.geometry for i in items]) if items else None

    def require(self):
        if self.errors:
            raise MissingGeometry('; '.join(self.errors))

    def covering(self, point):
        self.require()
        if self.tree is None:
            return []
        ids = self.tree.query(point, predicate='covered_by')
        return [self.items[int(i)] for i in sorted(ids)]

    def nearest(self, point):
        self.require()
        if self.tree is None:
            return None
        ids, distances = self.tree.query_nearest(point, return_distance=True, all_matches=True)
        # Stable tie-break for reproducible reports.
        choices = sorted((float(d), self.items[int(i)].feature.id, int(i)) for i, d in zip(ids, distances))
        distance, _, index = choices[0]
        return distance, self.items[index].feature


class SiteGeometry:
    def __init__(self, drawing: Drawing):
        self.drawing = drawing
        self.doc = read_document(drawing.source_bytes)
        self.cache = {}
        self.feature_cache = {}
        self.global_errors = []
        unknown = [f.id for f in drawing.features if not f.is_annotation and not f.type_id]
        if unknown:
            self.global_errors.append('Не классифицирована исходная геометрия: '+', '.join(unknown[:5])+f' (всего {len(unknown)}).')
        if any(d['code'] == 'unresolved_xref' for d in drawing.diagnostics):
            self.global_errors.append('Есть неразрешённые внешние ссылки DXF.')

    def field(self, type_ids, roles, *, confirmed_absent=(), require_area=False, feature_ids=None):
        key = (tuple(type_ids), tuple(roles), tuple(sorted(confirmed_absent)), require_area,
               None if feature_ids is None else tuple(sorted(feature_ids)))
        if key in self.cache:
            return self.cache[key]
        features = [f for f in self.drawing.features if not f.is_annotation and f.type_id in type_ids]
        errors = list(self.global_errors)
        if feature_ids is not None:
            missing = set(feature_ids) - {f.id for f in features}
            if missing: errors.append('Не найдены объекты выбранной области: '+', '.join(sorted(missing)))
            features = [f for f in features if f.id in feature_ids]
        items = []
        for f in features:
            if f.type_id in confirmed_absent:
                errors.append(f'{f.id}: объект присутствует, но его категория объявлена отсутствующей.')
            if f.geometry_role not in roles:
                errors.append(f'{f.id}: роль {f.geometry_role!r}; требуется {" / ".join(roles)}.')
                continue
            if f.id not in self.feature_cache:
                try:
                    entity = self.doc.entitydb.get(f.handle)
                    if entity is None:
                        raise MissingGeometry('Исходная сущность не найдена по handle.')
                    self.feature_cache[f.id] = _geometry(entity, f, self.drawing.metres_per_unit)
                except Exception as exc:
                    self.feature_cache[f.id] = MissingGeometry(str(exc))
            geom = self.feature_cache[f.id]
            if isinstance(geom, Exception):
                errors.append(f'{f.id}: {geom}')
            elif require_area and geom.geom_type not in {'Polygon', 'MultiPolygon'}:
                errors.append(f'{f.id}: требуется площадь, а не линия или точка.')
            else:
                items.append(GeometryItem(f, geom))
        absent = not features and set(type_ids) <= set(confirmed_absent)
        if not features and not absent:
            errors.append('Нет сведений о категории: '+', '.join(type_ids)+'. Отсутствие объектов нужно подтвердить отдельно.')
        result = GeometryField(items, errors, absent)
        self.cache[key] = result
        return result
