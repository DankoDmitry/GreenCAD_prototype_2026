"""Editor commands and undo/redo, entirely independent of a window or DXF API."""
from __future__ import annotations
from dataclasses import asdict
from copy import deepcopy
import uuid
from .models import Drawing, Placement, ProjectType, finite
from .catalog import default_types
from .errors import EditorError


class EditorSession:
    def __init__(self, drawing: Drawing, reference=None):
        from .reference_data import default_bundle
        self.reference = reference or default_bundle()
        self._references = {self.reference.id: self.reference}
        self.reference_notice = ''
        self.drawing = drawing
        self.types = {t.id: t for t in default_types()}
        self.types.update({t.id: deepcopy(t) for t in drawing.imported_types})
        self.placements = {p.id: deepcopy(p) for p in drawing.placements}
        from .checks import default_settings
        self.check_settings = default_settings(self.reference)
        self.validation_report = None  # Derived snapshot, not an edit command.
        self.selected_id: str | None = None
        self._history: list[dict] = [self.state()]
        self._index = 0
        self._saved = deepcopy(self._history[0])

    def state(self) -> dict:
        return {"types": [asdict(t) for t in self.types.values()],
                "placements": [asdict(p) for p in self.placements.values()],
                "layer_mapping": deepcopy(self.drawing.layer_mapping),
                "check_settings": deepcopy(self.check_settings),
                "reference_id": self.reference.id}

    def _apply_mapping(self) -> None:
        for f in self.drawing.features:
            if f.classification_source in {"XDATA", "ANNOTATION"}:
                continue
            spec = self.drawing.layer_mapping.get(f.layer, {})
            f.type_id, f.geometry_role = spec.get("type_id"), spec.get("geometry_role")
            f.classification_source = "LAYER_MAP" if f.type_id else "UNCLASSIFIED"
            for p in f.primitives:
                if p.closed and len(p.paths) == 1:
                    p.fill = bool(f.geometry_role in {"AREA", "OUTER_CONTOUR"} or f.entity_type in {"HATCH", "SOLID", "MPOLYGON"})

    def restore(self, state: dict, *, mark_saved: bool = False) -> None:
        types = [ProjectType.from_dict(d) for d in state.get("types", [])]
        placements = [Placement.from_dict(d) for d in state.get("placements", [])]
        if not types or len({t.id for t in types}) != len(types):
            raise EditorError("Типы проекта отсутствуют или содержат повторяющиеся ID.")
        if len(placements) > 20000 or len({p.id for p in placements}) != len(placements):
            raise EditorError("Посадок более 20 000 или повторяются ID.")
        type_ids = {t.id for t in types}
        if any(p.type_id not in type_ids for p in placements):
            raise EditorError("Посадка ссылается на неизвестный проектный тип.")
        mapping = state.get("layer_mapping", {})
        if not isinstance(mapping, dict) or any(not isinstance(k, str) or not isinstance(v, dict) for k, v in mapping.items()):
            raise EditorError("Неверная таблица соответствия слоёв.")
        from .checks import validate_settings, default_settings
        rid = state.get('reference_id', self.reference.id)
        if rid not in self._references:
            raise EditorError('Снимок справочника проекта не найден.')
        reference = self._references[rid]
        settings = validate_settings(state.get("check_settings", default_settings(reference)), reference)
        from .templates import validate_type_reference
        for t in types: validate_type_reference(t, reference)
        self.reference = reference
        self.check_settings = settings
        self.types = {t.id: t for t in types}
        self.placements = {p.id: p for p in placements}
        self.drawing.layer_mapping = deepcopy(mapping)
        self._apply_mapping()
        if self.selected_id not in self.placements:
            self.selected_id = None
        if mark_saved:
            self._history = [self.state()]
            self._index = 0
            self._saved = deepcopy(self._history[0])

    @property
    def dirty(self) -> bool:
        return self.state() != self._saved

    def mark_saved(self) -> None:
        self._saved = self.state()

    def _commit(self) -> None:
        state = self.state()
        if state == self._history[self._index]:
            return
        self._history = self._history[:self._index+1]
        self._history.append(state)
        if len(self._history) > 101:
            self._history.pop(0)
        self._index = len(self._history)-1

    def undo(self) -> bool:
        if self._index == 0:
            return False
        self._index -= 1
        self.restore(self._history[self._index])
        return True

    def redo(self) -> bool:
        if self._index+1 >= len(self._history):
            return False
        self._index += 1
        self.restore(self._history[self._index])
        return True

    @property
    def can_undo(self):
        return self._index > 0

    @property
    def can_redo(self):
        return self._index+1 < len(self._history)

    def add(self, type_id: str, x: float, y: float) -> Placement:
        if type_id not in self.types:
            raise EditorError("Выберите существующий проектный тип.")
        if len(self.placements) >= 20000:
            raise EditorError("Лимит редактора: 20 000 ручных посадок.")
        from .templates import new_template
        root_mode = (self.types[type_id].template or new_template())["root_protection"]
        p = Placement(uuid.uuid4().hex, type_id, finite(x, "X"), finite(y, "Y"), root_protection=root_mode == "REQUIRED")
        self.placements[p.id] = p
        self.selected_id = p.id
        self._commit()
        return p

    def move(self, placement_id: str, x: float, y: float) -> None:
        p = self.placements[placement_id]
        if p.locked:
            return
        p.x, p.y = finite(x, "X"), finite(y, "Y")
        self._commit()

    def change(self, placement_id: str, **fields) -> None:
        if not fields.keys() <= {"type_id", "x", "y", "plant_id", "root_protection", "locked", "label", "properties"}:
            raise EditorError("Неизвестное свойство посадки.")
        p = Placement.from_dict({**asdict(self.placements[placement_id]), **fields})
        if p.type_id not in self.types:
            raise EditorError("Неизвестный проектный тип.")
        old = self.placements[placement_id]
        if old.locked and p.locked and (p.x != old.x or p.y != old.y):
            raise EditorError("Сначала снимите фиксацию положения посадки.")
        self.placements[placement_id] = p
        self._commit()

    def delete(self, placement_id: str) -> None:
        if placement_id in self.placements:
            del self.placements[placement_id]
            self.selected_id = None
            self._commit()

    def duplicate(self, placement_id: str) -> Placement:
        old = self.placements[placement_id]
        if len(self.placements) >= 20000:
            raise EditorError("Лимит редактора: 20 000 посадок.")
        data = asdict(old)
        data.update(id=uuid.uuid4().hex, x=old.x+1, y=old.y+1, locked=False)
        p = Placement.from_dict(data)
        self.placements[p.id] = p
        self.selected_id = p.id
        self._commit()
        return p

    def set_type(self, project_type: ProjectType, *, clear_overrides: bool = False) -> None:
        project_type.validate()
        from .templates import validate_type_reference
        validate_type_reference(project_type, self.reference)
        self.types[project_type.id] = deepcopy(project_type)
        if clear_overrides:
            for p in self.placements.values():
                if p.type_id == project_type.id:
                    p.plant_id = None
        self._commit()

    def copy_type(self, type_id: str) -> ProjectType:
        if type_id not in self.types: raise EditorError('Проектный тип не найден.')
        n = 1
        while f'T{n}' in self.types: n += 1
        result = deepcopy(self.types[type_id])
        result.id, result.name = f'T{n}', result.name + ' — копия'
        # A copied legacy type keeps its own A1 range as an explicit new-profile range.
        legacy_range = self.check_settings.get('type_ranges', {}).get(type_id)
        if legacy_range is not None and result.template is not None and result.template['moisture_range'] is None:
            result.template['moisture_range'] = deepcopy(legacy_range)
        self.set_type(result)
        return result

    def delete_type(self, type_id: str) -> None:
        if type_id not in self.types: raise EditorError('Проектный тип не найден.')
        if len(self.types) <= 1: raise EditorError('Нельзя удалить последний тип.')
        if any(p.type_id == type_id for p in self.placements.values()):
            raise EditorError('Тип используется посадками. Сначала замените их тип или удалите посадки.')
        del self.types[type_id]
        self.check_settings['type_ranges'].pop(type_id, None)
        self._commit()

    def set_mapping(self, mapping: dict) -> None:
        self.drawing.layer_mapping = deepcopy(mapping)
        self._apply_mapping()
        self.drawing.diagnostics = [d for d in self.drawing.diagnostics if d["code"] != "unclassified"]
        missing = sum(f.classification_source == "UNCLASSIFIED" for f in self.drawing.features)
        if missing:
            self.drawing.diagnostics.append({"level":"WARNING", "code":"unclassified", "handle":"", "message":f"Без классификации: {missing}."})
        self._commit()

    def set_check_settings(self, settings: dict) -> None:
        from .checks import validate_settings
        self.check_settings = validate_settings(settings, self.reference)
        self._commit()

    def set_reference(self, reference) -> None:
        """Explicit update; history retains old snapshots without copying megabytes."""
        from .reference_data import ReferenceBundle
        from .checks import validate_settings
        if not isinstance(reference, ReferenceBundle):
            raise EditorError('Требуется проверенный пакет справочников.')
        settings = validate_settings(self.check_settings, reference)
        from .templates import validate_type_reference
        for t in self.types.values(): validate_type_reference(t, reference)
        known = {p['id'] for p in reference.plants()}
        used = {p.plant_id for p in self.placements.values()} | {t.plant_id for t in self.types.values()}
        missing = (used - {None}) - known
        if missing:
            raise EditorError('Новый пакет не содержит используемые растения: '+', '.join(sorted(missing)))
        self._references[reference.id] = reference
        self.reference = reference
        self.check_settings = settings
        self.reference_notice = 'Пакет обновлён явно. Для записи снимка на диск сохраните .gcp.'
        self._commit()
