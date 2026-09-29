from __future__ import annotations
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import json
import tempfile
import unittest
import zipfile

from greencad_editor.models import ProjectType
from greencad_editor.templates import (new_template, validate_template, validate_type_reference,
    match_plant, match_catalog, required_inputs)
from greencad_editor.reference_data import default_bundle, ReferenceBundle
from greencad_editor.errors import EditorError
from greencad_editor.demo import make_demo
from greencad_editor.dxf_io import load_dxf, export_dxf
from greencad_editor.session import EditorSession
from greencad_editor.checks import make_request,validate_plan,fingerprint,PointEvaluator,default_settings
from greencad_editor.project_io import save_project,load_project
from greencad_editor.admissibility import MapOptions,MapKernel,make_map_request,build_map,map_fingerprint

PID='PER.perspective.conifer_tree.004'
OTHER='REC.main.conifer_tree.001'

class Values:
    """Toy observations for numeric matching only; never saved in real catalogue."""
    def __init__(self,reference,**values):self.reference=reference;self.overrides=values
    def __getattr__(self,key):return getattr(self.reference,key)
    def value(self,pid,key,context=None):
        if key in self.overrides:
            v=self.overrides[key]
            return {'plant_id':pid,'parameter_id':key,'status':'KNOWN' if v is not None else 'UNKNOWN',
                    'value':v,'context':context or {},'source_id':'TEST','locator':'synthetic test only'}
        return self.reference.value(pid,key,context)

class TemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.reference=default_bundle()
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)
        self.s=EditorSession(load_dxf(make_demo(self.path/'site.dxf')))
        self.t=deepcopy(self.s.types['T1']);self.t.template=new_template()
    def tearDown(self):self.tmp.cleanup()
    def use(self,*ids,**site):
        self.s.set_type(self.t);settings=deepcopy(self.s.check_settings);settings['enabled']=list(ids);settings['site'].update(site);self.s.set_check_settings(settings)
    def check(self):return validate_plan(make_request(self.s))
    def row(self):return self.check()['placements'][0]
    def filter(self,key,op,val,context=None):
        self.t.template['filters']=[{'parameter_id':key,'operator':op,'value':val,'context':context or {}}]
    def test_plan_only_selection_does_not_make_template_map_pass(self):
        self.t.plant_id=PID;self.t.template['root_protection']='REQUIRED'
        self.use('R.spacing')
        options=MapOptions('T1',root_protection=True,step_m=10)
        m=build_map(make_map_request(self.s,options),options)
        self.assertEqual(m.kernel.inspect(20,10)['status'],'NOT_CHECKED')
        self.assertEqual(m.kernel.rule_ids,[])

    def test_two_requirement_origins_preserve_their_own_status(self):
        # Explicitly non-demanding synthetic plant + demanding project profile.
        self.t.plant_id=PID;self.t.template['requirements']['plant.drained_soil_required']=True
        self.use('R.domain','R.soil.drained',soil_drained=False);p=self.s.add('T1',20,10)
        from test_reference import changed_value
        request=make_request(self.s);request.reference=changed_value(self.reference)
        rows=validate_plan(request)['placements'][0]['checks']
        r=next(r for r in rows if r['rule_id']=='R.soil.drained')
        self.assertEqual(r['status'],'FAIL')
        self.assertEqual([v['status'] for v in r['evidence']['components']],['NOT_APPLICABLE','FAIL'])

    def test_invalid_operation_type_is_editor_error(self):
        self.t.template['filters']=[{'parameter_id':'plant.class','operator':[], 'value':'tree'}]
        with self.assertRaises(EditorError):validate_type_reference(self.t,self.reference)

    def test_cli_candidate_report_uses_saved_snapshot(self):
        self.s.set_type(self.t);path=self.path/'types.gcp';save_project(self.s,path)
        from tools.match_plants import main
        from unittest.mock import patch
        with patch('builtins.print'):
            code=main([str(path),'--type','T1','--output',str(self.path/'candidates.json')])
        self.assertEqual(code,0);report=json.loads((self.path/'candidates.json').read_text())
        self.assertEqual(len(report['candidates']),329);self.assertEqual(report['reference_sha256'],self.reference.id)

    def test_legacy_mode_not_changed(self):
        self.s.add('T1',40,33);r=self.row();self.assertEqual(r['status'],'PASS');self.assertEqual(len(r['checks']),3)
    def test_empty_optional_filters_do_not_require_unknown_sizes(self):
        r=match_plant(self.t,PID,self.reference);self.assertEqual(r['status'],'PASS');self.assertEqual(len(r['checks']),1)
    def test_unknown_size_does_not_pass(self):
        self.t.template['limits']['crown_radius_m']=1.5
        r=match_plant(self.t,PID,self.reference);self.assertEqual(r['status'],'UNKNOWN')
        self.assertEqual(r['checks'][1]['required'],3.)
    def test_numeric_bound_uses_diameter_not_radius(self):
        self.t.template['limits']['crown_radius_m']=1.5
        for value,status in [(2.9,'PASS'),(3.,'PASS'),(3.1,'FAIL'),(None,'UNKNOWN')]:
            r=match_plant(self.t,PID,Values(self.reference,**{'project.crown_diameter':value}))
            self.assertEqual(r['status'],status)
    def test_height_and_root_radius_are_distinct_from_symbol(self):
        self.t.radius_m=200;self.t.template['limits']={'height_max_m':1.5,'root_radius_m':1.0}
        ref=Values(self.reference,**{'project.total_height':1.5,'project.root_radius':.9})
        self.assertEqual(match_plant(self.t,PID,ref)['status'],'PASS')
        ref=Values(self.reference,**{'project.total_height':1.51,'project.root_radius':.9})
        self.assertEqual(match_plant(self.t,PID,ref)['status'],'FAIL')
    def test_categories_and_missing_flower_group(self):
        self.t.category='herbaceous';rows=match_catalog(self.t,self.reference)
        self.assertEqual(len(rows),329);self.assertFalse(any(r['status']=='PASS' for r in rows))
        self.t.category='shrub';self.assertEqual(match_plant(self.t,PID,self.reference)['status'],'FAIL')
    def test_boolean_filter_unknown_and_false_are_distinct(self):
        self.filter('plant.deicer_sensitive','eq',False)
        for v,status in [(None,'UNKNOWN'),(False,'PASS'),(True,'FAIL')]:
            self.assertEqual(match_plant(self.t,PID,Values(self.reference,**{'plant.deicer_sensitive':v}))['status'],status)
    def test_requirements_compare_strength_not_equality(self):
        self.t.template['requirements']['plant.drained_soil_required']=True
        self.assertEqual(match_plant(self.t,PID,Values(self.reference,**{'plant.drained_soil_required':False}))['status'],'PASS')
        self.t.template['requirements']['plant.drained_soil_required']=False
        self.assertEqual(match_plant(self.t,PID,self.reference)['status'],'FAIL')
    def test_unknown_property_retains_source_and_not_guessed(self):
        self.t.template['requirements']['plant.drained_soil_required']=True
        a=match_plant(self.t,OTHER,self.reference);self.assertEqual(a['status'],'UNKNOWN')
        b=match_plant(self.t,PID,self.reference);self.assertEqual(b['checks'][1]['value_record']['id'],'V02205')
    def test_interval_containment_is_not_overlap(self):
        self.filter('plant.soil_ph_range','between',[5,7])
        for actual,status in [([5.5,6.5],'PASS'),([4,6],'UNKNOWN'),([7.1,8],'FAIL')]:
            self.assertEqual(match_plant(self.t,PID,Values(self.reference,**{'plant.soil_ph_range':actual}))['status'],status)
    def test_interval_covering_and_set_filter(self):
        self.filter('plant.soil_ph_range','covers',[5.5,6.5]);self.assertEqual(match_plant(self.t,PID,Values(self.reference,**{'plant.soil_ph_range':[5,7]}))['status'],'PASS')
        self.filter('plant.fruit_near_exclusion','contains',['яблоня']);self.assertEqual(match_plant(self.t,PID,Values(self.reference,**{'plant.fruit_near_exclusion':['яблоня','груша']}))['status'],'PASS')
    def test_context_explicit(self):
        self.filter('plant.territory_mark','eq','MINUS',{'territory_id':'roads'})
        self.assertEqual(match_plant(self.t,PID,self.reference)['status'],'PASS')
        self.t.template['filters'][0]['context']={}
        with self.assertRaises(EditorError):validate_type_reference(self.t,self.reference)
    def test_profile_validation_is_strict(self):
        bads=[]
        for key,v in [('limits',{'crown_radius_m':float('nan')}),('limits',{'crown_radius_m':True}),('limits',{'crown_radius_m':-1}),('root_protection',[]),('schema_version',True),('moisture_range',[.8,.1]),('moisture_range',[.5,None]),('filters',[{'parameter_id':'unknown','operator':'eval','value':'__import__("os")'}])]:
            profile=new_template();profile[key]=v;bads.append(profile)
        for bad in bads:
            with self.assertRaises(EditorError):validate_template(bad,self.reference)
    def test_unknown_registered_parameter_and_operator_rejected(self):
        self.filter('plant.imaginary','eq',True)
        with self.assertRaises(EditorError):validate_type_reference(self.t,self.reference)
        self.filter('plant.drained_soil_required','le',1)
        with self.assertRaises(EditorError):validate_type_reference(self.t,self.reference)
    def test_profile_not_written_to_catalogue(self):
        before=self.reference.to_bytes();self.t.template['limits']['crown_radius_m']=3
        match_catalog(self.t,self.reference);self.assertEqual(before,self.reference.to_bytes())
    def test_type_requirement_without_plant(self):
        self.t.template['requirements']['plant.drained_soil_required']=True
        self.use('R.domain','R.soil.drained',soil_drained=True);self.s.add('T1',20,10)
        self.assertEqual(self.row()['status'],'PASS');self.assertEqual(self.row()['calculation_target'],'TEMPLATE')
        self.assertEqual(self.row()['checks'][1]['evidence']['calculation_origin'],'PROJECT_TYPE')
    def test_missing_active_template_input_is_unknown(self):
        self.use('R.domain','R.soil.drained',soil_drained=True);self.s.add('T1',20,10)
        self.assertEqual(self.row()['status'],'UNKNOWN')
    def test_explicit_no_requirement_is_not_unknown(self):
        self.t.template['requirements']['plant.drained_soil_required']=False
        self.use('R.domain','R.soil.drained');self.s.add('T1',20,10)
        self.assertEqual(self.row()['status'],'PASS');self.assertEqual(self.row()['checks'][1]['status'],'NOT_APPLICABLE')
    def test_real_plant_requirement_not_hidden_by_template_false(self):
        self.t.plant_id=PID;self.t.template['requirements']['plant.drained_soil_required']=False
        self.use('R.domain','R.soil.drained',soil_drained=False);self.s.add('T1',20,10)
        checks=self.row()['checks'];self.assertEqual(checks[1]['status'],'FAIL');self.assertEqual(checks[-1]['rule_id'],'template.catalog_match');self.assertEqual(checks[-1]['status'],'FAIL')
    def test_real_plant_unknown_not_filled_from_template(self):
        self.t.plant_id=OTHER;self.t.template['requirements']['plant.drained_soil_required']=True
        self.use('R.domain','R.soil.drained',soil_drained=True);self.s.add('T1',20,10)
        self.assertEqual(self.row()['status'],'UNKNOWN')
    def test_known_plant_can_supply_unset_type_requirement(self):
        self.t.plant_id=PID;self.use('R.domain','R.soil.drained',soil_drained=True);self.s.add('T1',20,10)
        self.assertEqual(self.row()['status'],'PASS')
    def test_profile_minimum_only_tightens(self):
        self.t.template['minimum_distances_m']['project.gas']=5
        self.use('project.gas');self.s.add('T1',40,34);self.assertEqual(self.row()['status'],'FAIL');self.assertEqual(self.row()['checks'][0]['required'],5)
        self.t.template['minimum_distances_m']['project.gas']=1;self.s.set_type(self.t)
        self.assertEqual(self.row()['checks'][0]['required'],3)
    def test_crown_reservation_and_symbol_radius(self):
        self.t.template['limits']['crown_radius_m']=1.5;self.t.radius_m=100
        self.use('R.domain');a=self.s.add('T1',3,10);self.assertEqual(self.row()['status'],'FAIL')
        self.s.move(a.id,3.5,10);self.assertEqual(self.row()['status'],'PASS')
    def test_root_bound_does_not_become_reserved_crown(self):
        self.t.template['limits']['root_radius_m']=10
        self.use('R.domain');self.s.add('T1',2.1,10);self.assertEqual(self.row()['status'],'PASS')
    def test_reserved_circle_does_not_enter_exclusions(self):
        self.t.template['limits']['crown_radius_m']=1.5
        self.use('project.excluded');a=self.s.add('T1',3,10);self.assertEqual(self.row()['status'],'FAIL')
        self.s.move(a.id,3.5,10);self.assertEqual(self.row()['status'],'PASS')
    def test_pair_spacing_is_symmetric_and_uses_both_radii(self):
        self.t.template['limits']['crown_radius_m']=2
        self.use('R.spacing');settings=deepcopy(self.s.check_settings);settings['thresholds_m']['R.spacing']=1;self.s.set_check_settings(settings)
        b=deepcopy(self.t);b.id='T2';b.template['limits']['crown_radius_m']=1;self.s.set_type(b)
        self.s.add('T1',20,10);self.s.add('T2',22.5,10)
        self.assertEqual(self.check()['counts'],{'FAIL':2})
        for row in self.check()['placements']:self.assertEqual(row['checks'][0]['evidence']['pair_conflicts'][0]['required'],3)
    def test_pair_minimum_from_other_type(self):
        self.t.template['minimum_distances_m']['R.spacing']=7;self.use('R.spacing')
        self.s.add('T1',20,10);self.s.add('T2',25,10);self.assertEqual(self.check()['counts'],{'FAIL':2})
    def test_root_mode_defaults_and_actual_violation(self):
        self.t.template['root_protection']='REQUIRED';self.use('R.domain')
        a=self.s.add('T1',20,10);self.assertTrue(a.root_protection);self.assertEqual(self.row()['status'],'PASS')
        self.s.change(a.id,root_protection=False);self.assertEqual(self.row()['checks'][-1]['status'],'FAIL')
    def test_root_forbidden_does_not_reduce_distance(self):
        self.t.template['root_protection']='FORBIDDEN';self.use('project.gas')
        a=self.s.add('T1',40,31);self.s.change(a.id,root_protection=True)
        self.assertEqual([c['status'] for c in self.row()['checks']],['FAIL','FAIL'])
    def test_A1_profile_and_legacy_intersection(self):
        self.t.template['moisture_range']=[.2,.8];self.use('R.moisture_range',A1=.5);self.s.add('T1',20,10)
        self.assertEqual(self.row()['status'],'PASS')
        settings=deepcopy(self.s.check_settings);settings['type_ranges']['T1']=[.6,1];self.s.set_check_settings(settings)
        self.assertEqual(self.row()['status'],'FAIL');self.assertEqual(self.row()['checks'][0]['required'],[.6,.8])
    def test_map_template_only_ignores_bound_plant_but_auto_does_not(self):
        self.t.template['requirements']['plant.drained_soil_required']=True;self.t.plant_id=OTHER
        self.use('R.domain','R.soil.drained',soil_drained=True)
        opt=MapOptions('T1',step_m=10,template_only=True)
        k=MapKernel(make_map_request(self.s,opt),opt);self.assertEqual(k.inspect(20,10)['status'],'PASS');self.assertIsNone(k.inspect(20,10)['plant_id'])
        m=build_map(make_map_request(self.s,opt),opt);self.assertIsNone(m.to_dict()['resolved_plant_id'])
        auto=MapOptions('T1',step_m=10);k=MapKernel(make_map_request(self.s,auto),auto);self.assertEqual(k.inspect(20,10)['status'],'UNKNOWN')
    def test_map_and_plan_parity_for_profile(self):
        self.t.template['requirements']['plant.drained_soil_required']=True;self.t.template['limits']['crown_radius_m']=1.5
        self.use('R.domain','project.gas','R.soil.drained',soil_drained=True)
        a=self.s.add('T1',3,10);b=self.s.add('T1',40,33)
        opt=MapOptions('T1',step_m=5);k=MapKernel(make_map_request(self.s,opt),opt)
        for row in self.check()['placements']:
            self.assertEqual(row['status'],k.inspect(row['x'],row['y'])['status'])
    def test_empty_selection_does_not_pass_due_to_automatic_checks(self):
        self.t.plant_id=PID;self.t.template['root_protection']='REQUIRED';self.use();self.s.add('T1',20,10)
        self.assertEqual(self.row()['status'],'NOT_CHECKED')
    def test_profile_and_match_readiness_does_not_mutate(self):
        self.t.template['requirements']['plant.drained_soil_required']=True;self.use('R.soil.drained')
        before=self.s.state();rows=required_inputs(self.t,self.reference.rules(),self.s.check_settings)
        self.assertEqual(rows[0]['value'],True);self.assertEqual(before,self.s.state())
    def test_history_and_copy_delete_types(self):
        self.s.set_type(self.t);new=self.s.copy_type('T1');self.assertNotEqual(new.id,'T1');self.assertEqual(new.template,self.t.template)
        self.s.delete_type(new.id);self.assertNotIn(new.id,self.s.types);self.s.undo();self.assertIn(new.id,self.s.types)
        self.s.add(new.id,20,10)
        with self.assertRaises(EditorError):self.s.delete_type(new.id)
    def test_profile_changes_invalidate_report_and_map(self):
        self.use('R.domain');self.s.add('T1',20,10);old=fingerprint(self.s);opt=MapOptions('T1');sig=map_fingerprint(self.s,opt)
        self.t.template['limits']['crown_radius_m']=3;self.s.set_type(self.t)
        self.assertNotEqual(old,fingerprint(self.s));self.assertNotEqual(sig,map_fingerprint(self.s,opt))
        self.s.undo();self.assertEqual(old,fingerprint(self.s));self.assertEqual(sig,map_fingerprint(self.s,opt))
    def test_atomic_rejection_of_bad_type(self):
        before=self.s.state();self.t.template['requirements']['invented.parameter']=True
        with self.assertRaises(EditorError):self.s.set_type(self.t)
        self.assertEqual(before,self.s.state())
    def test_gcp4_and_backwards_v3(self):
        self.t.template['limits']['crown_radius_m']=2;self.s.set_type(self.t);self.s.add('T1',20,10)
        p=self.path/'new.gcp';save_project(self.s,p);new,_=load_project(p);self.assertEqual(self.s.state(),new.state())
        with zipfile.ZipFile(p) as z:
            files={n:z.read(n) for n in z.namelist()};manifest=json.loads(files['project.json']);self.assertEqual(manifest['schema_version'],4)
        manifest['schema_version']=3
        for t in manifest['state']['types']:t.pop('template',None)
        files['project.json']=json.dumps(manifest).encode()
        with zipfile.ZipFile(self.path/'old.gcp','w') as z:
            for k,v in files.items():z.writestr(k,v)
        old,_=load_project(self.path/'old.gcp');self.assertIsNone(old.types['T1'].template);self.assertEqual(old.reference.id,self.s.reference.id)
    def test_dxf_type_and_source_roundtrip(self):
        self.t.template['requirements']['plant.drained_soil_required']=True;self.t.template['limits']['crown_radius_m']=1.5;self.s.set_type(self.t)
        self.s.add('T1',20,10);out=self.path/'out.dxf';rep=export_dxf(self.s.drawing,list(self.s.placements.values()),self.s.types,out)
        self.assertTrue(rep['source_preservation']['passed']);new=EditorSession(load_dxf(out));self.assertEqual(new.types['T1'].template,self.t.template)
        second=self.path/'second.dxf';export_dxf(new.drawing,list(new.placements.values()),new.types,second)
        self.assertEqual(len(load_dxf(second).placements),1)
    def test_reference_size_not_backfilled_by_template(self):
        self.t.template['limits']['crown_radius_m']=1.5;self.s.set_type(self.t)
        self.assertEqual(self.s.reference.value(PID,'project.crown_diameter')['status'],'UNKNOWN')
    def test_exported_profile_rejected_by_old_version_marker(self):
        self.s.set_type(self.t);self.s.add('T1',20,10);out=self.path/'out.dxf';export_dxf(self.s.drawing,list(self.s.placements.values()),self.s.types,out)
        import ezdxf
        d=ezdxf.readfile(out);record=d.rootdict['GREENCAD_EDITOR_PROFILE_V1']
        payload=json.loads(''.join(str(v.value) for v in record.tags if v.code==1));self.assertEqual(payload['schema_version'],2)
    def test_map_rejects_ambiguous_target(self):
        self.s.set_type(self.t)
        with self.assertRaises(EditorError):MapOptions('T1',PID,template_only=True).checked(self.s.types)

if __name__=='__main__':unittest.main()
