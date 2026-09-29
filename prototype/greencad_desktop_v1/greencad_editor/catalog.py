"""Read-only reference adapters. No normative or botanical assumptions here."""
from __future__ import annotations
import json
from pathlib import Path
from .models import ProjectType
from .errors import EditorError

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def read_json(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8-sig") as stream:
            return json.load(stream)
    except (ValueError, OSError) as exc:
        raise EditorError(f"Не удалось прочитать {path.name}: {exc}") from exc


def load_plants(reference=None) -> list[dict]:
    from .reference_data import default_bundle
    return (reference or default_bundle()).plants()


def load_object_types(reference=None) -> dict[str, dict]:
    from .reference_data import default_bundle
    return (reference or default_bundle()).objects()


def default_types() -> list[ProjectType]:
    return [ProjectType.from_dict(d) for d in read_json(DATA / "project_types.json")["types"]]


def default_mapping() -> dict[str, dict]:
    return read_json(DATA / "layer_mapping.json")["layers"]
