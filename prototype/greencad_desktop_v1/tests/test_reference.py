from __future__ import annotations
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import xml.etree.ElementTree as ET

from greencad_editor.reference_data import (default_bundle,compile_bundle,ReferenceBundle,canonical,
    read_bundle,write_bundle,parse_json)
from greencad_editor.reference_build import (build_from_sources,source_inputs,apply_workbook,REG,SITE)
from greencad_editor.session import EditorSession
from greencad_editor.dxf_io import load_dxf
from greencad_editor.demo import make_demo
from greencad_editor.checks import make_request,validate_plan,fingerprint,report_envelope
from greencad_editor.admissibility import MapOptions,build_map,make_map_request,map_fingerprint
from greencad_editor.project_io import save_project,load_project
from greencad_editor.errors import EditorError

ROOT=Path(__file__).resolve().parents[1]
PID='PER.perspective.conifer_tree.004'


def changed_value(bundle,value=False):
    p=bundle.to_dict()
    for v in p['registry']['values']:
        if v['id']=='V02205':v['value']=value
    return compile_bundle(p['registry'],p['site_dictionary'],p['implementation_profile'],origins=p['origins'],locations=p['locations'])


def changed_cell(raw,sheet_no,coord,value,kind='b'):
    out=io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src,zipfile.ZipFile(out,'w',compression=zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data=src.read(item.filename)
            if item.filename==f'xl/worksheets/sheet{sheet_no}.xml':
                root=ET.fromstring(data);ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                c=root.find('.//m:c[@r="'+coord+'"]',ns)
                if c is None:raise AssertionError(coord)
                for el in list(c):c.remove(el)
                c.set('t',kind)
                ET.SubElement(c,'{'+ns['m']+'}v').text=value
                data=ET.tostring(root,encoding='utf-8',xml_declaration=True)
            dst.writestr(item.filename,data)
    return out.getvalue()


class ReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.bundle=default_bundle()
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.folder=Path(self.tmp.name)
        self.s=EditorSession(load_dxf(make_demo(self.folder/'site.dxf')))
        p=self.s.add('T1',40,10);self.s.change(p.id,plant_id=PID);self.pid=p.id
        settings=deepcopy(self.s.check_settings);settings['enabled']=['R.domain','R.soil.drained'];settings['site']['soil_drained']=False
        self.s.set_check_settings(settings)
    def tearDown(self):self.tmp.cleanup()
    def result(self):return validate_plan(make_request(self.s))

    def test_default_build_is_reproducible(self):
        self.assertEqual(build_from_sources().to_bytes(),self.bundle.to_bytes())

    def test_windows_crlf_profile_build_matches(self):
        path=self.folder/'check_rules.json'
        raw=(ROOT/'data'/'check_rules.json').read_text(encoding='utf-8')
        path.write_bytes(raw.replace('\n','\r\n').encode('utf-8'))
        self.assertEqual(build_from_sources(profile_path=path).to_bytes(),self.bundle.to_bytes())

    def test_source_parity_of_all_values(self):
        raw=parse_json(source_inputs()[REG+'.json'])
        loaded={r['id']:r for r in self.bundle.table('values')}
        self.assertEqual(len(loaded),2863)
        for v in raw['values']:
            for key in ('plant_id','parameter_id','context','value','status','source_id','note_id','raw'):
                self.assertEqual(v[key],loaded[v['id']][key],(v['id'],key))

    def test_sources_and_display_copy_cannot_mutate_snapshot(self):
        copy=self.bundle.to_dict();copy['registry']['values'][0]['value']='MINUS'
        self.assertEqual(self.bundle.value('REC.main.conifer_tree.001','plant.territory_mark',{'territory_id':'courtyard'})['value'],'PLUS')
        with self.assertRaises(AttributeError):self.bundle.id='bad'
        copy=self.bundle.plants();copy[0]['name']='different'
        self.assertNotEqual(self.bundle.plants()[0]['name'],'different')

    def test_excel_location_is_exact(self):
        r=self.bundle.value(PID,'plant.drained_soil_required')
        self.assertEqual(r['id'],'V02205');self.assertEqual(r['location']['sheet'],'03_Значения')
        self.assertEqual(r['location']['cells']['value'],'E2210')
        self.assertEqual(self.bundle.plant(PID)['name'],'Тсуга канадская')

    def test_sizes_stay_unknown_not_symbol_size(self):
        self.assertEqual(self.bundle.value(PID,'project.root_radius')['status'],'UNKNOWN')
        self.assertEqual(self.bundle.value(PID,'project.crown_diameter')['status'],'UNKNOWN')
        self.assertEqual(self.bundle.value(PID,'plant.name')['value'],'Тсуга канадская')

    def test_typed_facts_not_flags_drive_both_maps_and_plan(self):
        p=self.bundle.to_dict()
        plant=next(x for x in p['registry']['plants'] if x['id']==PID);plant['flags']=[]
        b=compile_bundle(p['registry'],p['site_dictionary'],p['implementation_profile'])
        self.s.set_reference(b)
        self.assertEqual(self.result()['status'],'FAIL')
        m=build_map(make_map_request(self.s,MapOptions('T1',PID,step_m=10)),MapOptions('T1',PID,step_m=10))
        r=m.kernel.inspect(40,10)
        self.assertEqual(r['status'],'FAIL')
        self.assertEqual(r['checks'][1]['evidence']['value_record']['id'],'V02205')

    def test_excel_mutation_updates_evaluator_without_changing_formula(self):
        sources=source_inputs();path=self.folder/'changed.xlsx'
        path.write_bytes(changed_cell(sources[REG+'.xlsx'],4,'E2210','0'))
        b=build_from_sources(registry_xlsx=path)
        self.assertIs(b.value(PID,'plant.drained_soil_required')['value'],False)
        self.assertNotEqual(b.id,self.bundle.id)
        self.assertEqual(self.result()['status'],'FAIL')
        self.s.set_reference(b)
        out=self.result();self.assertEqual(out['status'],'PASS')
        self.assertEqual(out['placements'][0]['checks'][1]['status'],'NOT_APPLICABLE')
        self.assertEqual(out['placements'][0]['checks'][1]['evidence']['value_record']['location']['file'],'changed.xlsx')

    def test_invalid_typed_value_refused(self):
        with self.assertRaises(EditorError):changed_value(self.bundle,'false')

    def test_unknown_preserved(self):
        p=self.bundle.to_dict();v=next(v for v in p['registry']['values'] if v['id']=='V02205');v.update(status='UNKNOWN',value=None)
        b=compile_bundle(p['registry'],p['site_dictionary'],p['implementation_profile']);self.s.set_reference(b)
        self.assertEqual(self.result()['status'],'UNKNOWN')

    def test_duplicate_values_refused(self):
        p=self.bundle.to_dict();v=deepcopy(p['registry']['values'][0]);v['id']='V_duplicate';p['registry']['values'].append(v)
        with self.assertRaises(EditorError):compile_bundle(p['registry'],p['site_dictionary'],p['implementation_profile'])

    def test_unresolved_source_and_parameter_refused(self):
        for field,value in [('source_id','MISSING'),('parameter_id','MISSING')]:
            p=self.bundle.to_dict();p['registry']['values'][0][field]=value
            with self.assertRaises(EditorError):compile_bundle(p['registry'],p['site_dictionary'],p['implementation_profile'])

    def test_malformed_field_binding_refused(self):
        p=self.bundle.to_dict();next(f for f in p['site_dictionary']['fields'] if f['id']=='F.gas_axis')['required_geometry_role']='UNKNOWN'
        with self.assertRaises(EditorError):compile_bundle(p['registry'],p['site_dictionary'],p['implementation_profile'])

    def test_field_parameters_compile_from_site_table(self):
        p=self.bundle.to_dict();next(f for f in p['site_dictionary']['fields'] if f['id']=='F.gas_axis')['object_type_ids']=['net.water']
        b=compile_bundle(p['registry'],p['site_dictionary'],p['implementation_profile'])
        self.assertEqual(b.rule('project.gas')['object_types'],['net.water'])

    def test_executable_definition_tampering_rejected(self):
        p=self.bundle.to_dict();p['executable_rules'][2]['threshold_m']=100
        with self.assertRaises(EditorError):ReferenceBundle(p)

    def test_no_legacy_json_reads_during_evaluation(self):
        req=make_request(self.s)
        with patch('greencad_editor.checks.read_json',side_effect=AssertionError('legacy read')),patch('greencad_editor.catalog.read_json',side_effect=AssertionError('legacy read')):
            self.assertEqual(validate_plan(req)['status'],'FAIL')

    def test_request_pins_old_snapshot_on_update(self):
        request=make_request(self.s);self.s.set_reference(changed_value(self.bundle))
        self.assertEqual(validate_plan(request)['status'],'FAIL')
        self.assertEqual(self.result()['status'],'PASS')

    def test_reference_update_invalidates_and_undo_restores(self):
        self.s.validation_report=self.result();options=MapOptions('T1',PID,step_m=10)
        before=map_fingerprint(self.s,options)
        self.s.set_reference(changed_value(self.bundle))
        self.assertEqual(report_envelope(self.s)['state'],'STALE')
        self.assertNotEqual(before,map_fingerprint(self.s,options))
        self.s.undo();self.assertEqual(report_envelope(self.s)['state'],'CURRENT')
        self.assertEqual(before,map_fingerprint(self.s,options))
        self.s.redo();self.assertEqual(self.result()['status'],'PASS')

    def test_project_snapshot_independent_of_installed_catalog(self):
        self.s.set_reference(changed_value(self.bundle));path=self.folder/'project.gcp';save_project(self.s,path)
        with patch('greencad_editor.reference_data.default_bundle',side_effect=AssertionError('must use pinned snapshot')):
            loaded,_=load_project(path);req=make_request(loaded)
            self.assertEqual(validate_plan(req)['status'],'PASS')
        self.assertEqual(loaded.reference.id,self.s.reference.id)
        self.assertEqual(loaded.state(),self.s.state())

    def test_corrupt_project_reference_rejected(self):
        path=self.folder/'project.gcp';save_project(self.s,path)
        with zipfile.ZipFile(path) as z:items={n:z.read(n) for n in z.namelist()}
        items['reference.json']=changed_value(self.bundle).to_bytes()
        with zipfile.ZipFile(path,'w') as z:
            for name,raw in items.items():z.writestr(name,raw)
        with self.assertRaises(EditorError):load_project(path)

    def test_explicit_defaults_never_overwrite_project_threshold(self):
        settings=deepcopy(self.s.check_settings);settings['thresholds_m']['project.gas']=7;self.s.set_check_settings(settings)
        p=self.bundle.to_dict();next(r for r in p['implementation_profile']['rules'] if r['id']=='project.gas')['threshold_m']=4
        b=compile_bundle(p['registry'],p['site_dictionary'],p['implementation_profile']);self.s.set_reference(b)
        self.assertEqual(self.s.check_settings['thresholds_m']['project.gas'],7)

    def test_old_v2_project_is_migrated(self):
        path=self.folder/'v2.gcp';state=self.s.state();state.pop('reference_id')
        payload={'schema':'greencad.desktop-project','schema_version':2,'source':{'name':'site.dxf','sha256':self.s.drawing.source_sha256,'metres_per_unit':1},'state':state}
        with zipfile.ZipFile(path,'w') as z:
            z.writestr('project.json',json.dumps(payload));z.writestr('source.dxf',self.s.drawing.source_bytes)
        s,_=load_project(path);self.assertEqual(s.check_settings,self.s.check_settings)
        self.assertIn('Старый проект',s.reference_notice)

    def test_bundle_file_roundtrip(self):
        path=self.folder/'bundle.json.gz';write_bundle(self.bundle,path)
        self.assertEqual(read_bundle(path).id,self.bundle.id)

    def test_no_automatic_normative_activation(self):
        self.assertFalse(self.bundle.summary()['normative_profile'])
        self.assertTrue(all(r['kind'] in ('PROJECT','PROJECT_TEST','SOURCE_CONDITION','ADVISORY') for r in self.bundle.rules()))
        self.assertEqual(self.bundle.rule('project.gas')['binding']['threshold_origin'],'PROJECT_TEST')

    def test_invalid_xlsx_formula_not_cached(self):
        sources=source_inputs();raw=changed_cell(sources[REG+'.xlsx'],4,'E2210','1')
        out=io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(raw)) as z,zipfile.ZipFile(out,'w') as target:
            for i in z.infolist():
                data=z.read(i.filename)
                if i.filename.endswith('sheet4.xml'):
                    tree=ET.fromstring(data);ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                    c=tree.find('.//m:c[@r="E2210"]',ns);ET.SubElement(c,'{'+ns['m']+'}f').text='TRUE()';data=ET.tostring(tree)
                target.writestr(i.filename,data)
        path=self.folder/'formula.xlsx';path.write_bytes(out.getvalue())
        with self.assertRaises(EditorError):build_from_sources(registry_xlsx=path)

    def test_export_cannot_overwrite_dxf(self):
        path=self.folder/'original.dxf';path.write_text('keep')
        with self.assertRaises(EditorError):write_bundle(self.bundle,path)
        self.assertEqual(path.read_text(),'keep')

    def test_all_7567_source_based_evaluations(self):
        from greencad_editor.checks import PointEvaluator
        from greencad_editor.models import Placement
        raw=parse_json(source_inputs()[REG+'.json'])
        facts={(v['plant_id'],v['parameter_id'],canonical(v['context'])):v for v in raw['values']}
        count=0
        for terr in raw['territories']:
            req=make_request(self.s);req.settings['site'].update(territory=terr['id'],ordinary_territory=True)
            engine=PointEvaluator(req)
            for plant in raw['plants']:
                p=Placement('probe','T1',20,10,plant['id'])
                got=engine.evaluate(p,['R.territory'])[0]
                record=facts.get((plant['id'],'plant.territory_mark',canonical({'territory_id':terr['id']})))
                expected={'PLUS':'PASS','MINUS':'WARNING'}.get(record['value'],'UNKNOWN') if record else 'UNKNOWN'
                self.assertEqual(got.status,expected);count+=1
        rules=[r for r in self.bundle.rules() if r['operator']=='source_boolean']
        for state in (True,False,None):
            req=make_request(self.s)
            for r in rules:req.settings['site'][r['site_parameter']]=state
            engine=PointEvaluator(req)
            for plant in raw['plants']:
                p=Placement('probe','T1',20,10,plant['id'])
                for r in rules:
                    record=facts.get((plant['id'],r['plant_parameter'],canonical({})))
                    expected='UNKNOWN'
                    if record and record['status']=='KNOWN':
                        if record['value'] is False:expected='NOT_APPLICABLE'
                        elif state is not None:expected='PASS' if state==r['expected'] else 'FAIL'
                    self.assertEqual(engine.evaluate(p,[r['id']])[0].status,expected);count+=1
        self.assertEqual(count,7567)

if __name__=='__main__':unittest.main()
