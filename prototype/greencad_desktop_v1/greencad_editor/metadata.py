"""Small, versioned XDATA contract. No parsing of displayed text or color codes."""
from __future__ import annotations
import json
from .errors import EditorError

APPID = "GREENCAD_EDIT_V1"
SCHEMA_VERSION = 1
MAX_PAYLOAD = 12000  # below the usual 16K XDATA entity limit (our app only)


def encode_metadata(entity, data: dict) -> None:
    value = dict(data)
    value["schema_version"] = 2 if data.get("project_type", {}).get("template") is not None else SCHEMA_VERSION
    raw = json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    if len(raw) > MAX_PAYLOAD:
        raise EditorError("Метаданные объекта слишком велики для XDATA; используйте свойства проекта.")
    if APPID not in entity.doc.appids:
        entity.doc.appids.new(APPID)
    # ASCII escaped JSON; each string is below DXF's 255-byte limit.
    entity.set_xdata(APPID, [(1000, raw[i:i+200]) for i in range(0, len(raw), 200)])


def decode_metadata(entity) -> dict | None:
    if not entity.has_xdata(APPID):
        return None
    tags = entity.get_xdata(APPID)
    if any(tag.code != 1000 for tag in tags):
        raise EditorError("В XDATA GreenCAD ожидаются строковые фрагменты JSON (код 1000).")
    raw = "".join(str(tag.value) for tag in tags)
    if len(raw) > MAX_PAYLOAD:
        raise EditorError("Превышен лимит метаданных GreenCAD.")
    try:
        result = json.loads(raw, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    except (ValueError, TypeError) as exc:
        raise EditorError("Повреждены метаданные GreenCAD XDATA.") from exc
    if not isinstance(result, dict) or result.get("schema_version") not in (1, 2):
        raise EditorError("Неподдерживаемая версия XDATA GreenCAD; автоматическое изменение отключено.")
    if result.get("role") not in {"asset", "placement"}:
        raise EditorError("Неизвестная роль метаданных GreenCAD.")
    return result
