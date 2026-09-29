"""Four anonymous project types and SIX MANUAL points; not an auto-layout routine.
All dimensions and offsets below are declared synthetic project assumptions.
No botanical values, source workbooks or the reference snapshot are altered.
"""
from __future__ import annotations
from copy import deepcopy
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from greencad_editor.models import ProjectType
from greencad_editor.templates import new_template, SOIL_REQUIREMENTS
from greencad_editor.dxf_io import load_dxf,export_dxf
from greencad_editor.session import EditorSession
from greencad_editor.project_io import save_project
from greencad_editor.checks import validate_plan,make_request,save_report


def main():
    root=Path(__file__).resolve().parents[1];out=root/'examples'
    session=EditorSession(load_dxf(out/'demo_block.dxf'))
    for tid,name,category,radius,gas,root_mode in [
        ('T1','Крупный — проектный шаблон','tree',1.5,3.,'FORBIDDEN'),
        ('T2','Средний А — проектный шаблон','tree',1.,4.,'ANY'),
        ('T3','Средний Б — проектный шаблон','tree',.8,3.,'REQUIRED'),
        ('T4','Малый — проектный шаблон','shrub',.4,3.,'ANY')]:
        profile=new_template();profile['limits']={'crown_radius_m':radius}
        profile['requirements']={key:True for key in SOIL_REQUIREMENTS}
        profile['moisture_range']=[.3,.7] if tid!='T3' else [.2,.6]
        profile['root_protection']=root_mode;profile['minimum_distances_m']={'project.gas':gas,'R.spacing':3.}
        profile['state_label']='Условный тестовый габарит; не размеры реального вида или сорта'
        session.set_type(ProjectType(tid,name,category,radius,
            {'T1':'#286443','T2':'#407F63','T3':'#5C7891','T4':'#9A7552'}[tid],template=profile))
    session.delete_type('T5')
    settings=deepcopy(session.check_settings)
    settings['enabled']=['R.domain','project.excluded','project.gas','R.spacing','R.soil.drained','R.soil.loose','R.moisture_range']
    settings['site'].update(A1=.5,soil_drained=True,soil_loose=True,soil_saline=False,soil_compacted=False,stagnant_water=False)
    session.set_check_settings(settings)
    for tid,x,y,label in [('T1',18,16,'T1-1'),('T1',61,43,'T1-2'),('T2',21,44,'T2-1'),
                           ('T3',55,13,'T3-1'),('T3',61,13,'T3-2'),('T4',30,16,'T4-1')]:
        p=session.add(tid,x,y);session.change(p.id,label=label)
    report=validate_plan(make_request(session))
    if report['status']!='PASS':raise RuntimeError('Synthetic template example failed: '+report['status'])
    save_project(session,out/'demo_templates.gcp')
    export_report=export_dxf(session.drawing,list(session.placements.values()),session.types,out/'demo_templates.dxf')
    save_report({'note':'Six prechosen MANUAL points, synthetic project conditions only.',
                 'validation':report,'export':export_report},out/'demo_templates.report.json')
    print('Created examples/demo_templates.gcp, .dxf, .report.json; '+report['status'])

if __name__=='__main__':main()
