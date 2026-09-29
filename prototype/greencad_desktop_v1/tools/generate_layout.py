"""Headless template generation with the same preview/apply/check/export path."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from greencad_editor.generation import GenerationOptions, GenerationTarget, generate_plan, apply_generation
from greencad_editor.checks import make_request, save_report, report_envelope
from greencad_editor.dxf_io import load_dxf, export_dxf
from greencad_editor.project_io import load_project, save_project
from greencad_editor.session import EditorSession
from greencad_editor.errors import EditorError


def main(argv=None):
    parser = argparse.ArgumentParser(description='Расставить расчётные типы без окна. Пороги TEST не становятся нормативами.')
    parser.add_argument('input', type=Path, help='GCP с шаблонами либо подготовленный DXF с их метаданными')
    parser.add_argument('--zone', required=True, help='ID зелёной зоны, например green_01')
    parser.add_argument('--type', dest='targets', action='append', required=True, metavar='T1:20', help='Тип:максимум НОВЫХ экземпляров; порядок аргументов задаёт очередь')
    parser.add_argument('--step', type=float, default=1.0, help='Шаг поиска в метрах')
    parser.add_argument('--layout', choices=('grid', 'natural'), default='grid', help='grid: прежние ряды; natural: свободная расстановка с отступами')
    parser.add_argument('--seed', type=int, default=1, help='Номер варианта natural, 0..4294967295')
    parser.add_argument('--settings', type=Path, help='Явные настройки проверок JSON (не реестр норм)')
    parser.add_argument('--metres-per-unit', type=float)
    parser.add_argument('--output', type=Path, required=True, help='Новый выходной DXF')
    parser.add_argument('--project-output', type=Path, help='Дополнительно сохранить принятый GCP')
    parser.add_argument('--overwrite', action='store_true', help='Разрешить замену выходных файлов, но не входного')
    args = parser.parse_args(argv)
    try:
        if args.output.suffix.lower() != '.dxf': raise EditorError('--output должен иметь расширение .dxf.')
        if args.project_output and args.project_output.suffix.lower() != '.gcp': raise EditorError('--project-output должен иметь расширение .gcp.')
        generation_path = args.output.with_suffix('.generation.json')
        checks_path = args.output.with_suffix('.checks.json')
        outputs = [args.output, generation_path, checks_path] + ([args.project_output] if args.project_output else [])
        resolved = [p.expanduser().resolve() for p in outputs]
        protected = {args.input.expanduser().resolve()}
        if args.settings: protected.add(args.settings.expanduser().resolve())
        if len(set(resolved)) != len(resolved) or protected.intersection(resolved):
            raise EditorError('Входные и выходные пути должны различаться.')
        if not args.overwrite and any(p.exists() for p in outputs):
            raise EditorError('Выходной файл уже существует. Выберите новый путь либо явно задайте --overwrite.')
        targets = []
        for value in args.targets:
            try: tid, count = value.rsplit(':', 1); count = int(count)
            except (ValueError, TypeError): raise EditorError('--type задаётся как T1:20.')
            targets.append(GenerationTarget(tid, count))
        session = (load_project(args.input)[0] if args.input.suffix.lower() == '.gcp' else
                   EditorSession(load_dxf(args.input, metres_per_unit=args.metres_per_unit)))
        if args.settings: session.set_check_settings(json.loads(args.settings.read_text(encoding='utf-8-sig')))
        options = GenerationOptions(args.zone, tuple(targets), args.step, args.layout, args.seed)
        result = generate_plan(make_request(session), options)
        save_report(result.export(), generation_path)
        if not result.can_apply:
            print(json.dumps({'status': result.report['status'], 'message': result.report.get('message'),
                              'report': str(generation_path)}, ensure_ascii=False))
            return 3
        apply_generation(session, result)
        export_info = export_dxf(session.drawing, list(session.placements.values()), session.types, args.output)
        save_report({**report_envelope(session), 'generation_job_id': result.report['job_id'],
                     'export': export_info}, checks_path)
        if args.project_output: save_project(session, args.project_output)
        print(json.dumps({'status': result.report['status'], 'new_count': len(result.placements),
                          'statistics': result.report['statistics'], 'output': str(args.output)}, ensure_ascii=False))
        return 0 if result.report['status'] == 'READY' else 2
    except (EditorError, ValueError, OSError, KeyError) as exc:
        print(str(exc), file=sys.stderr); return 4

if __name__ == '__main__': raise SystemExit(main())
