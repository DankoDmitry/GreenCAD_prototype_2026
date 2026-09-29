"""Portable .gcp projects: ZIP with immutable source.dxf and versioned project.json.
No pickle, eval, arbitrary extraction, executable metadata, or external source dependency.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import tempfile
import zipfile
from .models import finite
from .dxf_io import load_bytes, sha256, MAX_DXF_BYTES
from .session import EditorSession
from .errors import EditorError
from .reference_data import ReferenceBundle, parse_json, MAX_BYTES as MAX_REFERENCE

PROJECT_SCHEMA = "greencad.desktop-project"
PROJECT_VERSION = 4
MAX_MANIFEST = 12 * 1024 * 1024


def save_project(session: EditorSession, path: str | Path, view: dict | None = None) -> None:
    path = Path(path).expanduser().resolve()
    if path.suffix.lower() != ".gcp":
        raise EditorError("Проект сохраняется в файл .gcp, а не поверх DXF.")
    payload = {"schema": PROJECT_SCHEMA, "schema_version": PROJECT_VERSION,
               "source": {"name": session.drawing.source_name, "sha256": session.drawing.source_sha256,
                          "metres_per_unit": session.drawing.metres_per_unit},
               "state": session.state(), "view": view or {},
               "validation": "RECHECK_AFTER_OPEN", "reference_sha256":session.reference.id}
    manifest = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    if len(manifest) > MAX_MANIFEST:
        raise EditorError("Слишком большой манифест проекта.")
    fd, tmp = tempfile.mkstemp(prefix=".greencad_", suffix=".gcp", dir=path.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr("project.json", manifest)
            z.writestr("source.dxf", session.drawing.source_bytes)
            z.writestr("reference.json", session.reference.to_bytes())
        with zipfile.ZipFile(tmp) as z:
            if z.testzip() is not None:
                raise EditorError("Не удалось проверить записанный проект.")
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
    session.mark_saved()


def load_project(path: str | Path) -> tuple[EditorSession, dict]:
    try:
        with zipfile.ZipFile(path) as z:
            names=set(z.namelist())
            if len(names)!=len(z.infolist()) or names not in ({"source.dxf","project.json"},{"source.dxf","project.json","reference.json"}):
                raise EditorError("Структура проекта неверна.")
            if "reference.json" in names and z.getinfo("reference.json").file_size>MAX_REFERENCE:
                raise EditorError("Снимок справочников превышает лимит.")
            if z.getinfo("source.dxf").file_size > MAX_DXF_BYTES or z.getinfo("project.json").file_size > MAX_MANIFEST:
                raise EditorError("Проект превышает лимит размера.")
            raw = z.read("source.dxf")
            manifest = parse_json(z.read("project.json"))
            reference_raw=z.read("reference.json") if "reference.json" in names else None
        if not isinstance(manifest, dict):
            raise EditorError("Манифест проекта должен быть объектом JSON.")
        if manifest.get("schema") != PROJECT_SCHEMA or manifest.get("schema_version") not in (1, 2, 3, PROJECT_VERSION):
            raise EditorError("Версия проекта не поддерживается. Файл не изменён.")
        source = manifest["source"]
        if sha256(raw) != source["sha256"]:
            raise EditorError("Не совпала контрольная сумма исходного DXF внутри проекта.")
        factor = finite(source["metres_per_unit"], "Масштаб проекта", positive=True)
        drawing = load_bytes(raw, source_name=Path(source["name"]).name, metres_per_unit=factor,
                             layer_mapping=manifest["state"].get("layer_mapping"))
        if manifest['schema_version']>=3:
            if reference_raw is None:
                raise EditorError('В проекте v3/v4 отсутствует снимок справочников.')
            reference=ReferenceBundle(parse_json(reference_raw))
            if reference.id!=manifest.get('reference_sha256') or manifest['state'].get('reference_id')!=reference.id:
                raise EditorError('Контрольная сумма справочника проекта не совпала.')
            session=EditorSession(drawing,reference=reference)
            session.reference_notice='Используется снимок справочника, сохранённый в этом .gcp.'
        else:
            if reference_raw is not None:raise EditorError('Лишний снимок в старом формате.')
            session=EditorSession(drawing)
            session.reference_notice='Старый проект не содержал справочников: подключён текущий пакет. Требуется повторная проверка.'
        session.restore(manifest["state"], mark_saved=True)
        return session, manifest.get("view", {})
    except EditorError:
        raise
    except (ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile) as exc:
        raise EditorError(f"Не удалось открыть проект: {exc}") from exc
