"""UI-independent domain types. World coordinates are XY in metres.

Visual radii are symbol sizes, NOT botanical measurements or normative buffers.
Optional properties are extension points, never executed as Python expressions.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any
import math
import re
from .errors import EditorError

Point = tuple[float, float]


def finite(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise EditorError(f"{name}: логический признак не является числом.")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise EditorError(f"{name}: требуется число.") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise EditorError(f"{name}: требуется конечное {'положительное ' if positive else ''}число.")
    return result


def required_id(value: Any, name: str = "id") -> str:
    if not isinstance(value, str) or not value or len(value) > 160 or any(ord(c) < 32 for c in value):
        raise EditorError(f"Некорректный {name}.")
    return value


def boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise EditorError(f"{name}: требуется true/false.")
    return value


@dataclass
class ProjectType:
    id: str
    name: str
    category: str = "tree"
    radius_m: float = 2.0
    color: str = "#327B55"
    plant_id: str | None = None
    symbol: str = "circle"
    properties: dict[str, Any] = field(default_factory=dict)
    template: dict[str, Any] | None = None

    def validate(self) -> None:
        required_id(self.id, "идентификатор типа")
        required_id(self.name, "название типа")
        self.radius_m = finite(self.radius_m, "Размер условного знака", positive=True)
        if self.radius_m > 10000:
            raise EditorError("Размер условного знака слишком велик (> 10 000 м).")
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", self.color):
            raise EditorError("Цвет задаётся как #RRGGBB.")
        if self.symbol not in {"circle", "point"}:
            raise EditorError("Условный знак: circle или point.")
        if self.plant_id is not None:
            required_id(self.plant_id, "plant_id")
        if not isinstance(self.properties, dict):
            raise EditorError("Свойства типа должны быть объектом JSON.")
        from .templates import validate_template
        self.template = validate_template(self.template)

    @classmethod
    def from_dict(cls, data: dict) -> 'ProjectType':
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        try:
            obj = cls(**known)
        except TypeError as exc:
            raise EditorError(f"Неполное описание проектного типа: {exc}") from exc
        obj.validate()
        return obj


@dataclass
class Placement:
    id: str
    type_id: str
    x: float
    y: float
    plant_id: str | None = None
    root_protection: bool = False
    locked: bool = False
    label: str = ""
    properties: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        required_id(self.id, "идентификатор посадки")
        required_id(self.type_id, "тип посадки")
        self.x = finite(self.x, "X")
        self.y = finite(self.y, "Y")
        boolean(self.root_protection, "Корнезащита")
        boolean(self.locked, "Фиксация")
        if not isinstance(self.label, str) or len(self.label) > 500:
            raise EditorError("Подпись должна быть строкой длиной до 500 символов.")
        if self.plant_id is not None:
            required_id(self.plant_id, "plant_id")
        if not isinstance(self.properties, dict):
            raise EditorError("Свойства посадки должны быть объектом JSON.")

    @classmethod
    def from_dict(cls, data: dict) -> 'Placement':
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        try:
            obj = cls(**known)
        except TypeError as exc:
            raise EditorError(f"Неполное описание посадки: {exc}") from exc
        obj.validate()
        return obj


@dataclass
class Primitive:
    """Display-only geometry; source DXF is NEVER recreated from these vertices."""
    kind: str
    paths: list[list[Point]] = field(default_factory=list)
    center: Point | None = None
    radius: float = 0.0
    text: str = ""
    text_height: float = 1.0
    angle: float = 0.0
    closed: bool = False
    fill: bool = False
    color: str | None = None

    def bounds(self) -> tuple[float, float, float, float]:
        cached = getattr(self, "_bounds_cache", None)
        if cached is not None:
            return cached
        points = [p for path in self.paths for p in path]
        if self.center is not None:
            x, y = self.center
            r = self.radius
            points.extend([(x-r, y-r), (x+r, y+r)])
        if not points:
            return (0.0, 0.0, 0.0, 0.0)
        result = (min(p[0] for p in points), min(p[1] for p in points),
                  max(p[0] for p in points), max(p[1] for p in points))
        self._bounds_cache = result
        return result


@dataclass
class Feature:
    id: str
    handle: str
    layer: str
    entity_type: str
    type_id: str | None
    geometry_role: str | None
    classification_source: str
    properties: dict[str, Any] = field(default_factory=dict)
    primitives: list[Primitive] = field(default_factory=list)
    is_annotation: bool = False


@dataclass
class Drawing:
    source_bytes: bytes
    source_name: str
    source_sha256: str
    metres_per_unit: float
    dxf_version: str
    features: list[Feature]
    placements: list[Placement]
    imported_types: list[ProjectType]
    layers: list[dict[str, Any]]
    diagnostics: list[dict[str, str]]
    layer_mapping: dict[str, dict[str, str]]
    source_disk_path: str | None = None
    modelspace_count: int = 0

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        values = [p.bounds() for f in self.features for p in f.primitives if p.kind != "text"]
        values.extend((p.x, p.y, p.x, p.y) for p in self.placements)
        if not values:
            return (0., 0., 80., 60.)
        x0, y0 = min(v[0] for v in values), min(v[1] for v in values)
        x1, y1 = max(v[2] for v in values), max(v[3] for v in values)
        return (x0, y0, max(x1, x0+1), max(y1, y0+1))


def to_dict(obj: Any) -> dict:
    return asdict(obj)
