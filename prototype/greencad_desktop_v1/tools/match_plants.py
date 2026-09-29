"""Export catalogue candidates for a saved type without a GUI or project changes."""
from __future__ import annotations
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from greencad_editor.project_io import load_project
from greencad_editor.templates import match_catalog
from greencad_editor.checks import save_report
from greencad_editor.errors import EditorError


def main(argv=None):
    parser=argparse.ArgumentParser(description='Подбор растений по условиям типа, не расстановка и не пространственная проверка.')
    parser.add_argument('input',type=Path,help='Проект .gcp со снимком справочника')
    parser.add_argument('--type',required=True,dest='type_id')
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args(argv)
    try:
        if args.output.resolve()==args.input.resolve():raise EditorError('Нельзя перезаписывать проект.')
        session,_=load_project(args.input)
        if args.type_id not in session.types:raise EditorError('Не найден проектный тип '+args.type_id)
        spec=session.types[args.type_id]
        rows=match_catalog(spec,session.reference)
        payload={'schema':'greencad.template-candidates','schema_version':1,'type_id':spec.id,
                 'template':spec.template,'category':spec.category,'reference_sha256':session.reference.id,
                 'scope':'Соответствие заданным критериям, не подтверждение пригодности участка.',
                 'counts':dict(Counter(r['status'] for r in rows)),'candidates':rows}
        save_report(payload,args.output)
        print(json.dumps({'counts':payload['counts'],'output':str(args.output)},ensure_ascii=False))
        return 0
    except (EditorError,OSError,ValueError) as exc:
        print(str(exc),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
