"""DXF boundary adapter. Source bytes are immutable; the preview is not a CAD kernel.

Only XDATA GREENCAD_EDIT_V1 or an explicit layer map provides semantic typing.
Annotations are never interpreted. Export starts again from the original bytes.
"""
from __future__ import annotations
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any
import hashlib
import json
import math
import os
import tempfile
import ezdxf
from ezdxf import path as ezpath, units
from ezdxf.colors import aci2rgb, int2rgb
from ezdxf.lldxf.tagwriter import TagCollector
from .models import Drawing, Feature, Primitive, Placement, ProjectType, finite
from .metadata import APPID, decode_metadata, encode_metadata
from .errors import EditorError, UnitsRequired
from .catalog import default_mapping

MAX_DXF_BYTES = 64 * 1024 * 1024
MAX_PREVIEW_ENTITIES = 100_000
MAX_PREVIEW_VERTICES = 600_000
ANNOTATIONS = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF", "DIMENSION", "LEADER", "MLEADER", "ACAD_TABLE"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_document(raw: bytes):
    if len(raw) > MAX_DXF_BYTES:
        raise EditorError("DXF превышает лимит этой сборки: 64 МиБ.")
    if not raw:
        raise EditorError("Пустой DXF.")
    # ezdxf.readfile detects ASCII/binary and the declared legacy encoding.
    # A temporary snapshot avoids writing or repairing the user's source file.
    with tempfile.TemporaryDirectory(prefix="greencad_read_") as tmp:
        path = Path(tmp) / "snapshot.dxf"
        path.write_bytes(raw)
        try:
            return ezdxf.readfile(path)
        except Exception as exc:
            raise EditorError(f"Не удалось прочитать DXF: {exc}") from exc


def scale_to_metres(doc, override: float | None = None) -> float:
    code = int(doc.header.get("$INSUNITS", 0))
    if code == 0:
        if override is None:
            raise UnitsRequired("В DXF не заданы единицы ($INSUNITS=0). Укажите метров в единице чертежа.")
        return finite(override, "Метров в единице чертежа", positive=True)
    try:
        factor = float(units.conversion_factor(code, units.M))
    except Exception as exc:
        raise EditorError(f"Не поддерживаются единицы DXF: код {code}.") from exc
    if override is not None and not math.isclose(factor, override, rel_tol=1e-12):
        raise EditorError("Заданный масштаб противоречит единицам самого DXF.")
    return factor


def _rgb(entity, doc) -> str:
    true_color = entity.dxf.get("true_color", None)
    if true_color is not None:
        rgb = int2rgb(true_color)
    else:
        aci = abs(int(entity.dxf.get("color", 256)))
        if aci in (0, 256):
            try:
                layer = doc.layers.get(entity.dxf.layer)
                if layer.dxf.hasattr("true_color"):
                    r, g, b = int2rgb(layer.dxf.true_color)
                    return f"#{r:02x}{g:02x}{b:02x}"
                aci = abs(layer.dxf.color)
            except Exception:
                aci = 7
        if aci == 7 or not 1 <= aci <= 255:
            return "#65717D"
        rgb = aci2rgb(aci)
    return f"#{rgb.r:02x}{rgb.g:02x}{rgb.b:02x}"


def load_dxf(path: str | Path, *, metres_per_unit: float | None = None,
             layer_mapping: dict | None = None) -> Drawing:
    path = Path(path).expanduser().resolve()
    if path.suffix.lower() != ".dxf":
        raise EditorError("Эта версия принимает DXF, а не DWG/ZIP. Преобразуйте DWG заранее.")
    try:
        if path.stat().st_size > MAX_DXF_BYTES:
            raise EditorError("DXF превышает лимит 64 МиБ.")
        raw = path.read_bytes()
    except OSError as exc:
        raise EditorError(f"Не удалось открыть {path}: {exc}") from exc
    return load_bytes(raw, source_name=path.name, source_disk_path=str(path),
                      metres_per_unit=metres_per_unit, layer_mapping=layer_mapping)


def load_bytes(raw: bytes, *, source_name: str = "source.dxf",
               source_disk_path: str | None = None, metres_per_unit: float | None = None,
               layer_mapping: dict | None = None) -> Drawing:
    doc = read_document(raw)
    embedded = {}
    key = "GREENCAD_EDITOR_PROFILE_V1"
    if key in doc.rootdict:
        try:
            record = doc.rootdict[key]
            embedded = json.loads("".join(str(t.value) for t in record.tags if t.code == 1))
            if embedded.get("schema_version") not in (1, 2):
                raise ValueError("schema_version")
        except Exception as exc:
            raise EditorError("Повреждён или не поддерживается профиль GreenCAD в DXF.") from exc
    factor = scale_to_metres(doc, metres_per_unit if metres_per_unit is not None else embedded.get("metres_per_unit"))
    mapping = dict(embedded.get("layer_mapping", default_mapping()) if layer_mapping is None else layer_mapping)
    features: list[Feature] = []
    placements: list[Placement] = []
    imported_types: dict[str, ProjectType] = {}
    for spec in embedded.get("project_types", []):
        item = ProjectType.from_dict(spec)
        imported_types[item.id] = item
    diagnostics: list[dict[str, str]] = []
    counts: Counter = Counter()
    seen_ids: set[str] = set()
    placement_ids: set[str] = set()
    vertex_count = 0
    digest = sha256(raw)

    def warn(code: str, text: str, handle: str = ""):
        diagnostics.append({"level": "WARNING", "code": code, "message": text, "handle": handle})

    def xy(v):
        x, y = float(v[0]) * factor, float(v[1]) * factor
        if not math.isfinite(x) or not math.isfinite(y):
            raise EditorError("Геометрия содержит NaN/Infinity.")
        if len(v) > 2 and abs(float(v[2])) > 1e-8:
            counts["nonzero_z"] += 1
        return (x, y)

    def flatten(p) -> list[tuple[float, float]]:
        nonlocal vertex_count
        result = []
        for v in p.flattening(distance=0.025/factor, segments=8):
            vertex_count += 1
            if vertex_count > MAX_PREVIEW_VERTICES:
                raise EditorError("Слишком много вершин для интерактивного предпросмотра (600 000).")
            result.append(xy(v))
        return result

    def primitives(entity, depth=0) -> list[Primitive]:
        if depth > 12:
            counts["block_depth_limit"] += 1
            return []
        kind = entity.dxftype()
        color = _rgb(entity, doc)
        if kind == "INSERT":
            result = []
            block = doc.blocks.get(entity.dxf.name)
            if block is None:
                counts["missing_block"] += 1
                return []
            if block.block.is_xref:
                counts["xref"] += 1
                return []
            inserts = list(entity.multi_insert()) if entity.mcount > 1 else [entity]
            for insert in inserts:
                def skipped(e, reason):
                    counts[f"virtual_skipped:{e.dxftype()}"] += 1
                for e in insert.virtual_entities(skipped_entity_callback=skipped):
                    result.extend(primitives(e, depth+1))
                for attr in insert.attribs:
                    result.extend(primitives(attr, depth+1))
            return result
        if kind == "LINE":
            return [Primitive("path", [[xy(entity.dxf.start), xy(entity.dxf.end)]], color=color)]
        if kind == "POINT":
            return [Primitive("point", center=xy(entity.dxf.location), color=color)]
        if kind == "CIRCLE" and tuple(entity.dxf.get("extrusion", (0, 0, 1))) == (0, 0, 1):
            return [Primitive("circle", center=xy(entity.dxf.center),
                              radius=abs(entity.dxf.radius)*factor, closed=True, color=color)]
        if kind in {"LWPOLYLINE", "POLYLINE", "CIRCLE", "ARC", "ELLIPSE", "SPLINE"}:
            p = ezpath.make_path(entity)
            paths = [flatten(sub) for sub in p.sub_paths()] if p.has_sub_paths else [flatten(p)]
            paths = [pts for pts in paths if len(pts) >= 2]
            closed = bool(p.is_closed)
            return [Primitive("path", paths, closed=closed, color=color)] if paths else []
        if kind in {"HATCH", "MPOLYGON"}:
            paths = [flatten(p) for p in ezpath.from_hatch(entity)]
            paths = [pts for pts in paths if len(pts) >= 3]
            if len(paths) > 1:
                counts["multi_boundary_fill"] += 1
            # With multiple boundaries we show outlines only; never fill across holes.
            return [Primitive("path", paths, closed=True, fill=len(paths)==1, color=color)] if paths else []
        if kind in {"SOLID", "TRACE", "3DFACE"}:
            pts = [xy(v) for v in entity.wcs_vertices()] if hasattr(entity, "wcs_vertices") else [xy(v) for v in entity.vertices()]
            return [Primitive("path", [pts], closed=True, fill=True, color=color)]
        if kind in {"TEXT", "ATTRIB", "ATTDEF", "MTEXT"}:
            text = entity.plain_text() if hasattr(entity, "plain_text") else str(entity.dxf.get("text", ""))
            h = entity.dxf.get("char_height", 1) if kind == "MTEXT" else entity.dxf.get("height", 1)
            return [Primitive("text", center=xy(entity.dxf.insert), text=text[:5000],
                              text_height=abs(h)*factor, angle=float(entity.dxf.get("rotation", 0)), color=color)]
        if kind in {"DIMENSION", "LEADER", "MLEADER"} and hasattr(entity, "virtual_entities"):
            result = []
            for e in entity.virtual_entities():
                result.extend(primitives(e, depth+1))
            return result
        counts[f"unsupported:{kind}"] += 1
        return []

    msp = doc.modelspace()
    total = len(msp)
    if total > MAX_PREVIEW_ENTITIES:
        raise EditorError(f"В modelspace {total} объектов; лимит редактора — {MAX_PREVIEW_ENTITIES}.")
    layer_counts = Counter()
    first_entity_by_layer = {}
    for entity in msp:
        handle = entity.dxf.handle
        kind = entity.dxftype()
        layer = entity.dxf.layer
        layer_counts[layer] += 1
        first_entity_by_layer.setdefault(layer, entity)
        try:
            meta = decode_metadata(entity)
        except EditorError as exc:
            # Do not accidentally rewrite a damaged or future managed result as background.
            raise EditorError(f"Объект {handle}: {exc}") from exc
        if meta and meta.get("role") == "placement":
            if kind not in {"CIRCLE", "POINT"}:
                raise EditorError(f"Посадка {handle}: ожидается CIRCLE или POINT, получен {kind}.")
            placement = Placement.from_dict(meta.get("placement", {}))
            t = ProjectType.from_dict(meta.get("project_type", {}))
            if placement.type_id != t.id:
                raise EditorError(f"Посадка {handle}: несогласованный type_id.")
            if placement.id in placement_ids:
                raise EditorError(f"Повторяется ID посадки {placement.id}. Копию в CAD необходимо снабдить новым ID.")
            placement_ids.add(placement.id)
            if t.id in imported_types and asdict(imported_types[t.id]) != asdict(t):
                raise EditorError(f"В DXF разные описания одного проектного типа {t.id}.")
            imported_types[t.id] = t
            # Geometry is authoritative after external CAD edits, not stale XY metadata.
            placement.x, placement.y = xy(entity.dxf.center if kind == "CIRCLE" else entity.dxf.location)
            placement.validate()
            if kind == "CIRCLE" and not math.isclose(entity.dxf.radius*factor, t.radius_m, rel_tol=1e-8):
                warn("symbol_size", f"Знак {placement.id} изменён в CAD. В редакторе применяется размер проектного типа.", handle)
            placements.append(placement)
            continue
        tag = meta if meta and meta.get("role") == "asset" else None
        spec = mapping.get(layer, {})
        if tag:
            asset_id = str(tag.get("id", ""))
            if not asset_id or asset_id in seen_ids:
                raise EditorError(f"Пустой или повторяющийся ID исходного объекта: {asset_id!r}.")
            seen_ids.add(asset_id)
            type_id, role = tag.get("type_id"), tag.get("geometry_role")
            if not isinstance(type_id, str) or not type_id or not isinstance(role, str) or not role:
                raise EditorError(f"Объект {asset_id}: нужны текстовые type_id и geometry_role.")
            props = tag.get("properties", {})
            if not isinstance(props, dict):
                raise EditorError(f"Объект {asset_id}: properties должен быть объектом JSON.")
            classification = "XDATA"
        else:
            asset_id = f"{digest[:12]}:{handle}"
            type_id, role = spec.get("type_id"), spec.get("geometry_role")
            props = {}
            classification = "LAYER_MAP" if type_id else "UNCLASSIFIED"
        annotation = kind in ANNOTATIONS
        if annotation:
            # Even mapped text is annotation. Text content NEVER supplies asset meaning.
            type_id, role, classification = None, None, "ANNOTATION"
        try:
            shapes = primitives(entity)
        except EditorError:
            raise
        except Exception as exc:
            warn("preview_geometry", f"{kind} {handle}: не показан в предпросмотре ({exc}). Исходник сохранён.", handle)
            shapes = []
        if role in {"AREA", "OUTER_CONTOUR"}:
            for p in shapes:
                if p.closed and len(p.paths)==1:
                    p.fill = True
        features.append(Feature(asset_id, handle, layer, kind, type_id, role, classification,
                                props, shapes, annotation))
    if counts["nonzero_z"]:
        warn("xy_projection", "Есть ненулевые Z: отображается горизонтальная проекция; исходные Z сохраняются в DXF.")
    if counts["multi_boundary_fill"]:
        warn("hatch_outlines", "Заливки с несколькими границами показаны только контурами (без закрашивания отверстий).")
    if counts["xref"]:
        warn("unresolved_xref", "Есть внешние ссылки: они не загружаются. Нужен заранее собранный DXF; предпросмотр неполон.")
    for code, count in counts.items():
        if code.startswith(("unsupported:", "virtual_skipped:")) or code in {"block_depth_limit", "missing_block"}:
            warn(code, f"Предпросмотр: {code}, объектов {count}. Они не удаляются из исходного документа.")
    missing = sum(f.classification_source == "UNCLASSIFIED" for f in features)
    if missing:
        warn("unclassified", f"Без установленного смысла: {missing}. Задайте точное соответствие слоёв; названия и тексты не угадываются.")
    layer_info = []
    for layer in doc.layers:
        name = layer.dxf.name
        layer_info.append({"name": name, "count": layer_counts[name],
                           "visible": not (layer.is_off() or layer.is_frozen()),
                           "color": _rgb(first_entity_by_layer.get(name, layer), doc)})
    return Drawing(raw, source_name, digest, factor, doc.dxfversion, features, placements,
                   list(imported_types.values()), layer_info, diagnostics, mapping,
                   source_disk_path=source_disk_path, modelspace_count=total)


def _value(v):
    if isinstance(v, bytes):
        return v.hex()
    if isinstance(v, (str, int, float)) or v is None:
        return v
    try:
        return [_value(x) for x in v]
    except TypeError:
        return str(v)


def entity_signature(entity, version: str) -> str:
    tags = TagCollector.dxftags(entity, dxfversion=version)
    return json.dumps([(t.code, _value(t.value)) for t in tags], ensure_ascii=True, separators=(",", ":"))


def source_signatures(doc) -> dict[str, str]:
    """Source graphical entities in model/paperspaces and block definitions, plus existing layers.
    Excludes only recognized managed placements; not all entities on result-named layers.
    Header timestamps/handles seed and dictionaries may necessarily change on save.
    """
    result = {}
    for block in doc.blocks:
        for entity in block:
            meta = decode_metadata(entity)
            if meta and meta.get("role") == "placement":
                continue
            if entity.dxf.handle:
                result[entity.dxf.handle] = entity_signature(entity, doc.dxfversion)
    for layer in doc.layers:
        result["layer:"+layer.dxf.handle] = entity_signature(layer, doc.dxfversion)
    return result


def verify_source_preserved(original_doc, exported_doc) -> dict:
    before, after = source_signatures(original_doc), source_signatures(exported_doc)
    missing = [key for key in before if key not in after]
    changed = [key for key in before if key in after and before[key] != after[key]]
    return {"passed": not missing and not changed, "source_records_checked": len(before),
            "missing": missing, "changed": changed,
            "scope": "Сериализованные DXF-теги исходных сущностей в блоках/пространствах и существующих слоев. Не побайтовое сравнение файлов."}


def export_dxf(drawing: Drawing, placements: list[Placement], types: dict[str, ProjectType],
               output_path: str | Path) -> dict:
    target = Path(output_path).expanduser().resolve()
    if target.suffix.lower() != ".dxf":
        raise EditorError("Экспорт должен иметь расширение .dxf.")
    if drawing.source_disk_path and os.path.normcase(str(target)) == os.path.normcase(str(Path(drawing.source_disk_path).resolve())):
        raise EditorError("Нельзя перезаписывать входной DXF. Выберите новое имя результата.")
    if target.is_file() and target.stat().st_size == len(drawing.source_bytes) and sha256(target.read_bytes()) == drawing.source_sha256:
        raise EditorError("Нельзя заменять копию исходного DXF. Выберите другое имя результата.")
    if not target.parent.is_dir():
        raise EditorError("Каталог экспорта не существует.")
    original = read_document(drawing.source_bytes)
    doc = read_document(drawing.source_bytes)
    if doc.dxfversion < "AC1015":
        raise EditorError("Экспорт этой сборки требует DXF R2000 или новее. Подготовьте исходник в R2010.")
    msp = doc.modelspace()
    for entity in list(msp):
        meta = decode_metadata(entity)
        if meta and meta.get("role") == "placement":
            msp.delete_entity(entity)
    ids = set()
    for p in placements:
        p.validate()
        if p.id in ids:
            raise EditorError("Повторяющийся ID посадки при экспорте.")
        ids.add(p.id)
        if p.type_id not in types:
            raise EditorError(f"Не определён проектный тип {p.type_id}.")
        t = types[p.type_id]
        t.validate()
        # Stable, ASCII-only layer names; do not alter an existing source layer's attributes.
        layer = "GC_RESULT_" + hashlib.sha256(t.id.encode()).hexdigest()[:10].upper()
        if layer not in doc.layers:
            doc.layers.new(layer, dxfattribs={"color": 3})
        color = int(t.color[1:], 16)
        pos = (p.x / drawing.metres_per_unit, p.y / drawing.metres_per_unit, 0)
        attrs = {"layer": layer, "true_color": color}
        entity = msp.add_point(pos, dxfattribs=attrs) if t.symbol == "point" else msp.add_circle(pos, t.radius_m/drawing.metres_per_unit, dxfattribs=attrs)
        encode_metadata(entity, {"role": "placement", "placement": asdict(p), "project_type": asdict(t),
                                  "geometry_role": "SYMBOL_CENTER", "validation": "RECHECK_REQUIRED"})
    key = "GREENCAD_EDITOR_PROFILE_V1"
    if key in doc.rootdict:
        record = doc.rootdict[key]
    else:
        record = doc.rootdict.add_xrecord(key)
    profile = json.dumps({"schema_version": 2 if any(t.template is not None for t in types.values()) else 1, "metres_per_unit": drawing.metres_per_unit, "layer_mapping": drawing.layer_mapping,
                          "project_types": [asdict(t) for t in types.values()]},
                         ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    record.reset([(1, profile[i:i+200]) for i in range(0, len(profile), 200)])
    fd, tmp_name = tempfile.mkstemp(prefix=".greencad_", suffix=".dxf", dir=target.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        doc.saveas(tmp_path)
        check = ezdxf.readfile(tmp_path)
        preservation = verify_source_preserved(original, check)
        if not preservation["passed"]:
            raise EditorError(f"Экспорт отменён: изменилась исходная сущность. {preservation}")
        reread = load_bytes(tmp_path.read_bytes(), source_name=target.name,
                            metres_per_unit=drawing.metres_per_unit, layer_mapping=drawing.layer_mapping)
        by_id = {p.id: p for p in reread.placements}
        if len(by_id) != len(placements):
            raise EditorError("Проверка экспорта: не совпало число посадок.")
        for p in placements:
            q = by_id.get(p.id)
            if q is None or not math.isclose(p.x, q.x, rel_tol=0.0, abs_tol=1e-8) or not math.isclose(p.y, q.y, rel_tol=0.0, abs_tol=1e-8):
                raise EditorError("Проверка экспорта: не совпали координаты посадок.")
        actual_types={t.id:asdict(t) for t in reread.imported_types}
        if actual_types!={tid:asdict(t) for tid,t in types.items()}:
            raise EditorError('Проверка экспорта: не совпали проектные типы и расчётные профили.')
        os.replace(tmp_path, target)
    finally:
        tmp_path.unlink(missing_ok=True)
    return {"status": "PASS", "input_sha256": drawing.source_sha256,
            "output_sha256": sha256(target.read_bytes()), "placements": len(placements),
            "source_preservation": preservation, "coordinate_check": "PASS", "normative_validation": "NOT_IMPLEMENTED",
            "output": target.name, "warnings": drawing.diagnostics}
