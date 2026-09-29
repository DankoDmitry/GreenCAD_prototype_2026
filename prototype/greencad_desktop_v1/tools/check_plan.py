"""Headless validator for the same settings/operators used by the desktop button."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from greencad_editor.project_io import load_project
from greencad_editor.dxf_io import load_dxf
from greencad_editor.session import EditorSession
from greencad_editor.checks import make_request,validate_plan,report_envelope,save_report
from greencad_editor.errors import EditorError


def main(argv=None):
    parser=argparse.ArgumentParser(description='Проверки заданных посадок; без окна и авторасстановки.')
    parser.add_argument('input',type=Path,help='DXF или GCP')
    parser.add_argument('--settings',type=Path,help='JSON настроек schema_version=1 (не реестр правил)')
    parser.add_argument('--output',type=Path,required=True,help='Файл отчёта JSON')
    args=parser.parse_args(argv)
    try:
        session=load_project(args.input)[0] if args.input.suffix.lower()=='.gcp' else EditorSession(load_dxf(args.input))
        if args.settings:session.set_check_settings(json.loads(args.settings.read_text(encoding='utf-8-sig')))
        report=validate_plan(make_request(session));session.validation_report=report
        save_report(report_envelope(session),args.output)
        print(json.dumps({'status':report['status'],'counts':report['counts'],'output':str(args.output)},ensure_ascii=False))
        if report['status'] in ('PASS','WARNING'):return 0
        return 2 if report['status']=='FAIL' else 3
    except (EditorError,ValueError,OSError) as exc:
        print(str(exc),file=sys.stderr);return 4

if __name__=='__main__':raise SystemExit(main())
