"""Headless map sampling, same operators as the Tk editor. No automatic planting."""
from __future__ import annotations
import argparse
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from greencad_editor.admissibility import MapOptions,make_map_request,build_map
from greencad_editor.checks import save_report,validate_settings
from greencad_editor.dxf_io import load_dxf
from greencad_editor.errors import EditorError
from greencad_editor.project_io import load_project
from greencad_editor.session import EditorSession
import json


def main(argv=None):
    parser=argparse.ArgumentParser(description='Карта допусков по выбранным условиям (не расстановка)')
    parser.add_argument('input',type=Path,help='Подготовленный DXF или проект .gcp')
    parser.add_argument('--type',default='T1',dest='type_id')
    parser.add_argument('--plant',default=None,help='ID растения; иначе — из проектного типа')
    parser.add_argument('--step',type=float,default=1.0,help='Шаг карты в метрах')
    parser.add_argument('--root-protection',action='store_true')
    parser.add_argument('--template-only',action='store_true',help='Проверять только профиль типа без назначенного растения')
    parser.add_argument('--settings',type=Path)
    parser.add_argument('--metres-per-unit',type=float)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if args.output.resolve()==args.input.resolve():
            raise EditorError('Нельзя перезаписывать входной файл.')
        if args.input.suffix.lower()=='.gcp':
            session,_=load_project(args.input)
        else:
            session=EditorSession(load_dxf(args.input,metres_per_unit=args.metres_per_unit))
        if args.settings:
            if args.output.resolve()==args.settings.resolve():
                raise EditorError('Нельзя перезаписывать файл настроек.')
            session.set_check_settings(validate_settings(json.loads(args.settings.read_text(encoding='utf-8')),session.reference))
        options=MapOptions(args.type_id,args.plant,args.root_protection,args.step,args.template_only).checked(session.types)
        result=build_map(make_map_request(session,options),options)
        save_report(result.to_dict(),args.output)
        print(json.dumps({'cells':result.grid.count,'sampled':result.sampled_count,'counts':result.counts(),
                          'excluded_plan_rules':result.kernel.excluded,'output':str(args.output)},ensure_ascii=False))
        # A completed map can contain forbidden cells; that is not a CLI error.
        return 0
    except (EditorError,ValueError,OSError) as exc:
        print(str(exc),file=sys.stderr)
        return 2

if __name__=='__main__':raise SystemExit(main())
