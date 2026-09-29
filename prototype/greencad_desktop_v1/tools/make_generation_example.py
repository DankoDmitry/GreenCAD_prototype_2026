"""Generate the example output using the real placement algorithm, not hand-picked points.

Input: existing demo_templates.gcp (four anonymous templates, six manual points).
Output: actual additive generation of up to 20 NEW points per template.
All offsets and template dimensions remain declared synthetic TEST conditions.
"""
from __future__ import annotations
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from greencad_editor.checks import make_request, save_report, report_envelope
from greencad_editor.generation import GenerationOptions, GenerationTarget, generate_plan, apply_generation
from greencad_editor.project_io import load_project, save_project
from greencad_editor.dxf_io import export_dxf


def main():
    root = Path(__file__).resolve().parents[1]
    session, _ = load_project(root/'examples/demo_templates.gcp')
    options = GenerationOptions('green_01', tuple(GenerationTarget(t, 20) for t in ('T1','T2','T3','T4')), 1.)
    result = generate_plan(make_request(session), options)
    if result is None or not result.can_apply:
        raise RuntimeError('Synthetic generation example failed.')
    apply_generation(session, result)
    output = root/'examples/demo_generated.dxf'
    exported = export_dxf(session.drawing, list(session.placements.values()), session.types, output)
    save_project(session, root/'examples/demo_generated.gcp')
    save_report(result.export(), root/'examples/demo_generated.generation.json')
    save_report({**report_envelope(session), 'export': exported}, root/'examples/demo_generated.checks.json')
    print(result.report['status'], result.report['statistics'])
    print('Preserved manual:', result.report['manual_count'], 'new:', len(result.placements),
          'elapsed:', result.report['elapsed_seconds'])

if __name__ == '__main__': main()
