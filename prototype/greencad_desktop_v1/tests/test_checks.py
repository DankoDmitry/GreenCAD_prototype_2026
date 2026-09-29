from __future__ import annotations
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch
import ezdxf
from greencad_editor.demo import make_demo
from greencad_editor.dxf_io import load_dxf
from greencad_editor.metadata import encode_metadata,decode_metadata
from greencad_editor.session import EditorSession
from greencad_editor.project_io import save_project,load_project
from greencad_editor.models import Primitive
from greencad_editor.errors import EditorError
from greencad_editor.checks import (make_request,validate_plan,default_settings,
    validate_settings,report_envelope,fingerprint,load_rules,save_report)


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.folder=Path(self.tmp.name)
        self.path=make_demo(self.folder/'исходник.dxf')
        self.s=EditorSession(load_dxf(self.path))

    def tearDown(self):self.tmp.cleanup()

    def select(self,*ids,**site):
        settings=deepcopy(self.s.check_settings);settings['enabled']=list(ids)
        settings['site'].update(site);self.s.set_check_settings(settings)

    def run_checks(self):
        r=validate_plan(make_request(self.s));self.s.validation_report=r;return r

    def reload_doc(self,fn):
        doc=ezdxf.readfile(self.path);fn(doc);doc.saveas(self.path)
        self.s=EditorSession(load_dxf(self.path))

    def test_default_scope_and_all_results(self):
        self.s.add('T1',20,10);self.s.add('T1',40,31);self.s.add('T1',-3,10)
        before=self.s.state();raw=self.path.read_bytes()
        r=self.run_checks()
        self.assertEqual([x['status'] for x in r['placements']],['PASS','FAIL','FAIL'])
        self.assertTrue(all(len(x['checks'])==3 for x in r['placements']))
        self.assertEqual(sum(c['status']=='FAIL' for c in r['placements'][2]['checks']),2)
        self.assertEqual(before,self.s.state());self.assertEqual(raw,self.path.read_bytes())
        self.assertEqual(r['placements'][1]['checks'][2]['object_ids'],['gas_01'])
        self.assertEqual(r['placements'][1]['checks'][2]['actual'],1)

    def test_exact_boundary_and_disabled_rule(self):
        for y in (32.999999,33,33.000001):self.s.add('T1',40,y)
        self.select('project.gas')
        self.assertEqual([r['status'] for r in self.run_checks()['placements']],['FAIL','PASS','PASS'])
        self.select('R.domain');self.assertEqual(self.run_checks()['counts'],{'PASS':3})

    def test_empty_plan_and_empty_selection(self):
        self.assertEqual(self.run_checks()['status'],'EMPTY')
        self.s.add('T1',20,10);self.select()
        self.assertEqual(self.run_checks()['status'],'NOT_CHECKED')
        self.assertEqual(self.s.validation_report['placements'][0]['status'],'NOT_CHECKED')

    def test_unknown_id_and_operator_not_silently_passed(self):
        self.s.add('T1',20,10);self.select('does.not.exist')
        self.assertEqual(self.run_checks()['placements'][0]['status'],'UNKNOWN')
        self.select('R.domain');rules=load_rules();rules[0]['operator']='unimplemented'
        r=validate_plan(make_request(self.s),rules=rules)
        self.assertEqual(r['placements'][0]['status'],'UNKNOWN')

    def test_handler_failure_does_not_abort_other_rules(self):
        self.s.add('T1',40,31)
        def broken(*args):raise RuntimeError('test handler failure')
        with patch('greencad_editor.checks.LOG.exception'):
            r=validate_plan(make_request(self.s),operators={'inside':broken})
        c=r['placements'][0]['checks']
        self.assertEqual([x['status'] for x in c],['ERROR','PASS','FAIL'])
        self.assertEqual(r['status'],'FAIL')

    def test_missing_geometry_and_confirmed_absence(self):
        self.reload_doc(lambda d:d.modelspace().delete_entity(next(e for e in d.modelspace() if e.dxf.layer=='GC_GAS_AXIS')))
        self.s.add('T1',40,30);self.select('project.gas')
        self.assertEqual(self.run_checks()['status'],'UNKNOWN')
        settings=deepcopy(self.s.check_settings);settings['confirmed_absent']=['net.gas'];self.s.set_check_settings(settings)
        self.assertEqual(self.run_checks()['status'],'PASS')

    def test_false_absence_conflicts_with_present_object(self):
        self.s.add('T1',40,40);self.select('project.gas')
        settings=deepcopy(self.s.check_settings);settings['confirmed_absent']=['net.gas'];self.s.set_check_settings(settings)
        self.assertEqual(self.run_checks()['status'],'UNKNOWN')

    def test_wrong_geometry_role(self):
        def modify(d):
            e=next(e for e in d.modelspace() if e.dxf.layer=='GC_GAS_AXIS')
            meta=decode_metadata(e);meta['geometry_role']='BOUNDARY';encode_metadata(e,meta)
        self.reload_doc(modify);self.s.add('T1',40,40);self.select('project.gas')
        self.assertEqual(self.run_checks()['status'],'UNKNOWN')

    def test_curves_not_replaced_by_preview(self):
        def modify(d):
            e=next(e for e in d.modelspace() if e.dxf.layer=='GC_GAS_AXIS')
            points=list(e.get_points());points[0]=(*points[0][:4],.2);e.set_points(points)
        self.reload_doc(modify);self.s.add('T1',40,40);self.select('project.gas')
        self.assertEqual(self.run_checks()['status'],'UNKNOWN')

    def test_ignores_display_primitives(self):
        for f in self.s.drawing.features:f.primitives=[Primitive('point',center=(10000,10000))]
        self.s.add('T1',40,31);self.select('project.gas')
        self.assertEqual(self.run_checks()['placements'][0]['checks'][0]['actual'],1.)

    def test_unclassified_geometry_blocks_geometry_verdict(self):
        self.reload_doc(lambda d:d.modelspace().add_line((10,10),(20,20)))
        self.s.add('T1',40,40);self.select('project.gas')
        self.assertEqual(self.run_checks()['status'],'UNKNOWN')

    def test_hatch_hole_preserved(self):
        def modify(d):
            m=d.modelspace();e=next(e for e in m if e.dxf.layer=='GC_GREEN_ZONE');meta=decode_metadata(e);m.delete_entity(e)
            h=m.add_hatch(dxfattribs={'layer':'GC_GREEN_ZONE'})
            h.paths.add_polyline_path([(2,2),(78,2),(78,58),(2,58)],is_closed=True,flags=1)
            h.paths.add_polyline_path([(10,10),(20,10),(20,20),(10,20)],is_closed=True,flags=0)
            encode_metadata(h,meta)
        self.reload_doc(modify);self.s.add('T1',15,15);self.s.add('T1',30,20);self.select('R.domain')
        self.assertEqual([r['status'] for r in self.run_checks()['placements']],['FAIL','PASS'])

    def test_open_domain_unknown(self):
        self.reload_doc(lambda d:setattr(next(e for e in d.modelspace() if e.dxf.layer=='GC_GREEN_ZONE'),'closed',False))
        self.s.add('T1',20,20);self.select('R.domain');self.assertEqual(self.run_checks()['status'],'UNKNOWN')

    def test_millimetres_large_origin_and_horizontal_projection(self):
        make_demo(self.path,millimetres=True,origin=(1230000,-870000))
        self.s=EditorSession(load_dxf(self.path));self.s.add('T1',1230040,-869969);self.select('project.gas')
        r=self.run_checks();self.assertEqual(r['status'],'FAIL');self.assertAlmostEqual(r['placements'][0]['checks'][0]['actual'],1)

    def test_polyline_and_circle_center_supported(self):
        def modify(d):
            m=d.modelspace();e=next(e for e in m if e.dxf.layer=='GC_GAS_AXIS');meta=decode_metadata(e);m.delete_entity(e)
            e=m.add_polyline2d([(-8,30),(40,30),(88,30)],dxfattribs={'layer':'GC_GAS_AXIS'});encode_metadata(e,meta)
        self.reload_doc(modify);self.s.add('T1',40,31);self.select('project.gas')
        self.assertEqual(self.run_checks()['status'],'FAIL')

    def test_pair_distance_reports_both_and_duplicates(self):
        a=self.s.add('T1',20,20);b=self.s.add('T2',20,20);self.s.change(b.id,locked=True)
        self.select('R.spacing');r=self.run_checks()
        self.assertEqual(r['counts'],{'FAIL':2})
        self.assertEqual(r['placements'][0]['checks'][0]['object_ids'],[b.id])
        self.assertEqual(r['placements'][1]['checks'][0]['object_ids'],[a.id])

    def test_moisture_unbound_and_scenario_ranges(self):
        self.s.add('T1',20,10);self.select('R.moisture_range')
        self.assertEqual(self.run_checks()['status'],'UNKNOWN')
        settings=deepcopy(self.s.check_settings);settings['site']['A1']=.5;settings['type_ranges']['T1']=[.4,.6]
        self.s.set_check_settings(settings);self.assertEqual(self.run_checks()['status'],'PASS')
        settings['site']['A1']=.8;self.s.set_check_settings(settings);self.assertEqual(self.run_checks()['status'],'FAIL')

    def test_catalog_note4_and_unknown_not_false(self):
        p=self.s.add('T1',20,10);self.s.change(p.id,plant_id='REC.main.conifer_tree.002')
        self.select('R.soil.drained',soil_drained=True)
        r=self.run_checks();self.assertEqual(r['status'],'PASS');self.assertEqual(r['placements'][0]['checks'][0]['sources'][0]['source_id'],'REC')
        self.select('R.soil.drained',soil_drained=False);self.assertEqual(self.run_checks()['status'],'FAIL')
        self.s.change(p.id,plant_id='REC.main.conifer_tree.001');self.assertEqual(self.run_checks()['status'],'UNKNOWN')

    def test_default_type_plant_used(self):
        from dataclasses import replace
        self.s.set_type(replace(self.s.types['T1'],plant_id='REC.main.conifer_tree.002'))
        self.s.add('T1',20,10);self.select('R.soil.drained',soil_drained=True)
        self.assertEqual(self.run_checks()['status'],'PASS')

    def test_recommendation_not_a_hard_failure(self):
        p=self.s.add('T1',20,10);self.s.change(p.id,plant_id='REC.main.conifer_tree.002')
        self.select('R.domain','R.territory',territory='roads',ordinary_territory=True)
        r=self.run_checks();self.assertEqual(r['status'],'WARNING')
        self.assertEqual(r['placements'][0]['checks'][1]['actual'],'-')
        self.select('R.territory',ordinary_territory=None);self.assertEqual(self.run_checks()['status'],'UNKNOWN')

    def test_report_invalidates_for_edits_not_view(self):
        p=self.s.add('T1',20,10);self.run_checks();self.assertEqual(report_envelope(self.s)['state'],'CURRENT')
        self.s.selected_id=None;self.assertEqual(report_envelope(self.s)['state'],'CURRENT')
        self.s.move(p.id,20,11);self.assertEqual(report_envelope(self.s)['state'],'STALE')
        self.s.undo();self.assertEqual(report_envelope(self.s)['state'],'CURRENT')
        self.select('R.domain');self.assertEqual(report_envelope(self.s)['state'],'STALE')

    def test_request_is_snapshot(self):
        p=self.s.add('T1',40,31);request=make_request(self.s);self.s.move(p.id,40,40)
        r=validate_plan(request);self.assertEqual(r['status'],'FAIL')
        self.assertNotEqual(r['fingerprint'],fingerprint(self.s))

    def test_saved_settings_and_v1_migration(self):
        self.s.add('T1',20,20);self.select('R.moisture_range',A1=.7)
        settings=deepcopy(self.s.check_settings);settings['type_ranges']['T1']=[.2,.8];self.s.set_check_settings(settings)
        self.run_checks();path=self.folder/'new.gcp';save_project(self.s,path)
        new,_=load_project(path);self.assertEqual(new.check_settings,self.s.check_settings)
        self.assertIsNone(new.validation_report)
        with zipfile.ZipFile(path) as z:self.assertEqual(json.loads(z.read('project.json'))['schema_version'],4)
        old,_=load_project(Path(__file__).resolve().parents[1]/'examples/demo_session.gcp')
        self.assertEqual(old.check_settings,default_settings())

    def test_invalid_settings_rejected_atomically(self):
        before=self.s.state()
        for value in (float('nan'),float('inf'),True,-1):
            s=deepcopy(self.s.check_settings);s['site']['A1']=value
            with self.assertRaises(EditorError):self.s.set_check_settings(s)
        s=deepcopy(self.s.check_settings);s['type_ranges']['T1']=[.8,.1]
        with self.assertRaises(EditorError):self.s.set_check_settings(s)
        self.assertEqual(before,self.s.state())

    def test_report_serializes_sources_and_no_nan(self):
        self.s.add('T1',20,10);self.run_checks();path=self.folder/'report.json'
        save_report(report_envelope(self.s),path)
        data=json.loads(path.read_text());self.assertEqual(data['state'],'CURRENT')
        self.assertEqual(data['report']['enabled_rules'],self.s.check_settings['enabled'])
        self.assertTrue(data['report']['placements'][0]['checks'][0]['sources'])
        with self.assertRaises(EditorError):save_report(data,self.folder/'input.dxf')

if __name__=='__main__':unittest.main()
