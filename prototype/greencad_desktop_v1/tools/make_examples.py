"""Rebuild synthetic files; not an automatic planting algorithm."""
from pathlib import Path
import json
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from greencad_editor.demo import make_demo
from greencad_editor.dxf_io import load_dxf,export_dxf
from greencad_editor.session import EditorSession
from greencad_editor.project_io import save_project
from greencad_editor.catalog import load_plants

root=Path(__file__).resolve().parents[1]
source=make_demo(root/'examples/demo_block.dxf')
s=EditorSession(load_dxf(source))
# Known manual test coordinates, deliberately unrelated to any suitability calculation.
for type_id,x,y,label in [('T1',18,16,'P1'),('T1',61,43,'P2'),('T2',21,44,'P3'),('T3',55,13,'P4'),('T3',61,13,'P5'),('T4',30,16,'P6')]:
    p=s.add(type_id,x,y);s.change(p.id,label=label)
report=export_dxf(s.drawing,list(s.placements.values()),s.types,root/'examples/demo_result.dxf')
(root/'examples/demo_result.export.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
save_project(s,root/'examples/demo_session.gcp')
print('Created demo_block.dxf, demo_result.dxf, demo_session.gcp')
