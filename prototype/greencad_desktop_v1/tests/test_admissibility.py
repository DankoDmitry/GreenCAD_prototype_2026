from __future__ import annotations
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from threading import Event
import tempfile
import unittest
from unittest.mock import patch

import ezdxf

from greencad_editor.admissibility import (MapOptions, GridSpec, MapKernel, CODES,
    TOTAL, build_map, make_map_request, map_fingerprint)
from greencad_editor.checks import (make_request, validate_plan, load_rules, CheckResult,
                                   aggregate)
from greencad_editor.demo import make_demo
from greencad_editor.dxf_io import load_dxf
from greencad_editor.errors import EditorError
from greencad_editor.metadata import encode_metadata
from greencad_editor.session import EditorSession


class AdmissibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.path = make_demo(self.folder/'demo.dxf')
        self.session = EditorSession(load_dxf(self.path))
        self.options = MapOptions('T1', step_m=4)

    def tearDown(self):
        self.tmp.cleanup()

    def settings(self, ids):
        settings = deepcopy(self.session.check_settings)
        settings['enabled'] = ids
        self.session.set_check_settings(settings)

    def build(self, **kwargs):
        return build_map(make_map_request(self.session, self.options), self.options, **kwargs)

    def test_parity_with_plan_all_cells(self):
        result = self.build()
        req = make_request(self.session)
        from greencad_editor.models import Placement
        req.placements = [Placement(str(ix)+':'+str(iy),'T1',*result.grid.center(ix,iy))
                          for iy in range(result.grid.ny) for ix in range(result.grid.nx)]
        report = validate_plan(req)
        for i, row in enumerate(report['placements']):
            self.assertEqual(result.combined[i], CODES[row['status']])
            for r in row['checks']:
                self.assertEqual(result.layers[r['rule_id']][i], CODES[r['status']])

    def test_readonly_no_hidden_placements(self):
        self.session.add('T1', 40, 31)
        before = deepcopy(self.session.state())
        previous = self.session.validation_report
        result = self.build()
        self.assertEqual(self.session.state(), before)
        self.assertIs(self.session.validation_report, previous)
        self.assertEqual(result.kernel.request.placements, [])
        self.assertGreater(result.sampled_count, 0)

    def test_exact_click_boundary_not_nearest_cell(self):
        self.options = MapOptions('T1', step_m=4)
        result = self.build()
        self.assertEqual(result.kernel.inspect(40, 32.999)['status'], 'FAIL')
        self.assertEqual(result.kernel.inspect(40, 33)['status'], 'PASS')
        self.assertEqual(result.kernel.inspect(40, 33.001)['status'], 'PASS')
        a=result.grid.locate(40,32.999);b=result.grid.locate(40,33.001)
        self.assertEqual(a,b)  # two opposite outcomes in one visual cell
        detail=result.kernel.inspect(40,31)
        gas=next(r for r in detail['checks'] if r['rule_id']=='project.gas')
        self.assertEqual(gas['actual'],1)
        self.assertEqual(gas['object_ids'],['gas_01'])

    def test_a1_unknown_fail_pass(self):
        self.settings(['R.moisture_range'])
        self.assertEqual(set(self.build().combined),{CODES['UNKNOWN']})
        s=deepcopy(self.session.check_settings)
        s['site']['A1']=.7;s['type_ranges']['T1']=[.2,.6]
        self.session.set_check_settings(s)
        self.assertEqual(set(self.build().combined),{CODES['FAIL']})
        s['site']['A1']=.6;self.session.set_check_settings(s)
        self.assertEqual(set(self.build().combined),{CODES['PASS']})

    def test_fail_not_masked_by_unknown(self):
        self.settings(['project.gas','R.moisture_range'])
        result=self.build()
        self.assertEqual(result.kernel.inspect(40,30)['status'],'FAIL')
        self.assertEqual(result.kernel.inspect(40,40)['status'],'UNKNOWN')

    def test_spacing_excluded_and_no_green_on_spacing_only(self):
        self.settings(['R.spacing'])
        result=self.build()
        self.assertEqual(set(result.combined),{CODES['NOT_CHECKED']})
        self.assertEqual(result.layers,{})
        self.assertEqual(result.kernel.excluded[0]['id'],'R.spacing')

    def test_positions_do_not_change_static_map_signature(self):
        self.settings(['R.domain','project.gas','R.spacing'])
        before=map_fingerprint(self.session,self.options)
        p=self.session.add('T1',40,31)
        self.session.move(p.id,40,40)
        self.assertEqual(before,map_fingerprint(self.session,self.options))
        result=self.build()
        self.assertNotIn('R.spacing',result.layers)
        self.assertEqual(result.kernel.inspect(40,40)['status'],'PASS')

    def test_relevant_changes_invalidate(self):
        before=map_fingerprint(self.session,self.options)
        s=deepcopy(self.session.check_settings);s['thresholds_m']['project.gas']=9
        self.session.set_check_settings(s)
        self.assertNotEqual(before,map_fingerprint(self.session,self.options))
        self.session.undo()
        self.assertEqual(before,map_fingerprint(self.session,self.options))
        t=deepcopy(self.session.types['T1']);t.plant_id='arbitrary'
        self.session.set_type(t)
        self.assertNotEqual(before,map_fingerprint(self.session,self.options))

    def test_missing_gas_not_absence(self):
        doc=ezdxf.readfile(self.path)
        for e in list(doc.modelspace()):
            if e.dxf.layer=='GC_GAS_AXIS':doc.modelspace().delete_entity(e)
        doc.saveas(self.path)
        self.session=EditorSession(load_dxf(self.path));self.settings(['project.gas'])
        self.assertEqual(set(self.build().combined),{CODES['UNKNOWN']})
        s=deepcopy(self.session.check_settings);s['confirmed_absent']=['net.gas']
        self.session.set_check_settings(s)
        self.assertEqual(set(self.build().combined),{CODES['PASS']})

    def test_plant_required_and_catalog_data_used(self):
        self.settings(['R.soil.drained'])
        rules=load_rules()
        # Use actual ID from the catalogue-backed boolean rule.
        rid=next(r['id'] for r in rules if r.get('site_parameter')=='soil_drained')
        self.settings([rid])
        self.assertEqual(set(self.build().combined),{CODES['UNKNOWN']})
        from greencad_editor.catalog import DATA,read_json
        facts=read_json(DATA/'plant_check_facts.json')
        pid=next(pid for pid,row in facts['plants'].items() if '4' in row[1])
        self.options=MapOptions('T1',pid,step_m=5)
        s=deepcopy(self.session.check_settings);s['site']['soil_drained']=False
        self.session.set_check_settings(s)
        self.assertEqual(set(self.build().combined),{CODES['FAIL']})
        s['site']['soil_drained']=True;self.session.set_check_settings(s)
        self.assertEqual(set(self.build().combined),{CODES['PASS']})

    def test_unknown_rule_handler_and_scope_no_false_green(self):
        r={'id':'plugin.new','label':'New','kind':'PROJECT','description':'Test','operator':'future'}
        self.settings([r['id']])
        for variant in (None,r,dict(r,evaluation_scope='SITE_POINT')):
            result=self.build(rules=[] if variant is None else [variant])
            self.assertEqual(set(result.combined),{CODES['UNKNOWN']})

    def test_same_plugin_interface(self):
        r={'id':'wind','label':'Wind','kind':'PROJECT','description':'Test',
           'operator':'wind','evaluation_scope':'SITE_POINT'}
        self.settings(['wind'])
        def wind(c,r,p):
            return c.outcome(r,'PASS' if p.x>30 else 'FAIL','Synthetic west effect',actual=p.x)
        result=self.build(rules=[r],operators={'wind':wind})
        self.assertEqual(result.kernel.inspect(20,20)['status'],'FAIL')
        self.assertEqual(result.kernel.inspect(40,20)['status'],'PASS')
        req=make_request(self.session)
        from greencad_editor.models import Placement
        req.placements=[Placement('p','T1',20,20)]
        self.assertEqual(validate_plan(req,rules=[r],operators={'wind':wind})['status'],'FAIL')

    def test_errors_preserved(self):
        r={'id':'broken','label':'Broken','kind':'PROJECT','description':'Test',
           'operator':'bad','evaluation_scope':'SITE_POINT'}
        self.settings(['broken']);self.options=MapOptions('T1',step_m=100)
        def bad(*args):raise RuntimeError('synthetic failure')
        with self.assertLogs('greencad_editor.checks',level='ERROR'):
            result=self.build(rules=[r],operators={'bad':bad})
        self.assertEqual(set(result.combined),{CODES['ERROR']})

    def test_empty_and_advisory_only_not_pass(self):
        self.settings([])
        self.assertEqual(set(self.build().combined),{CODES['NOT_CHECKED']})
        r={'id':'advice','label':'Advice','kind':'ADVISORY','description':'Test',
           'operator':'advice','evaluation_scope':'SITE_POINT'}
        self.settings(['advice'])
        def advise(c,r,p):return c.outcome(r,'WARNING','Test advice')
        result=self.build(rules=[r],operators={'advice':advise})
        self.assertEqual(set(result.combined),{CODES['NOT_CHECKED']})
        self.assertEqual(set(result.layers['advice']),{CODES['WARNING']})

    def test_advisory_unknown_does_not_override_mandatory_map(self):
        rules=load_rules()+[{'id':'advice','label':'Advice','kind':'ADVISORY',
                            'description':'Test','operator':'advice','evaluation_scope':'SITE_POINT'}]
        self.settings(['R.domain','advice'])
        def advice(c,r,p):return c.outcome(r,'UNKNOWN','No advice data')
        result=self.build(rules=rules,operators={'advice':advice})
        self.assertEqual(set(result.combined),{CODES['PASS']})
        self.assertEqual(set(result.layers['advice']),{CODES['UNKNOWN']})

    def test_coarse_grid_with_no_centres_is_not_impossibility_proof(self):
        doc=ezdxf.readfile(self.path)
        for e in list(doc.modelspace()):
            if e.dxf.layer=='GC_GREEN_ZONE':doc.modelspace().delete_entity(e)
        h=doc.modelspace().add_hatch();h.dxf.layer='GC_GREEN_ZONE'
        h.paths.add_polyline_path([(0,0),(20,0),(20,20),(0,20)],is_closed=True)
        h.paths.add_polyline_path([(8,8),(12,8),(12,12),(8,12)],is_closed=True)
        encode_metadata(h,{'role':'asset','id':'hole','type_id':'context.green_zone',
                           'geometry_role':'AREA','properties':{}})
        doc.saveas(self.path);self.session=EditorSession(load_dxf(self.path))
        self.options=MapOptions('T1',step_m=50);result=self.build()
        self.assertEqual(result.sampled_count,0)
        self.assertEqual(result.counts(),{})
        self.assertTrue(result.kernel.contains(5,5))

    def test_cancel_before_and_during(self):
        event=Event();event.set()
        self.assertIsNone(self.build(cancel=event))
        event.clear()
        calls=[]
        def progress(n,total):calls.append(n);event.set()
        self.assertIsNone(self.build(cancel=event,progress=progress))
        self.assertEqual(len(calls),1)

    def test_grid_limit_finiteness_and_edge_centres(self):
        for step in (0,-1,float('nan'),float('inf'),True,1e-6):
            with self.subTest(step=step),self.assertRaises(EditorError):
                GridSpec.create((0,0,80,60),step)
        grid=GridSpec.create((0,0,10,5),3)
        self.assertEqual(grid.center(3,1),(9.5,4))
        self.assertEqual(grid.locate(10,5),(3,1))
        self.assertIsNone(grid.locate(10.1,5))

    def test_holes_and_disjoint_zones_are_transparent(self):
        doc=ezdxf.readfile(self.path)
        for e in list(doc.modelspace()):
            if e.dxf.layer=='GC_GREEN_ZONE':doc.modelspace().delete_entity(e)
        h=doc.modelspace().add_hatch()
        h.dxf.layer='GC_GREEN_ZONE'
        h.paths.add_polyline_path([(2,2),(22,2),(22,22),(2,22)],is_closed=True)
        h.paths.add_polyline_path([(8,8),(16,8),(16,16),(8,16)],is_closed=True)
        h.paths.add_polyline_path([(40,2),(48,2),(48,10),(40,10)],is_closed=True)
        encode_metadata(h,{'role':'asset','id':'green_holes','type_id':'context.green_zone',
                           'geometry_role':'AREA','properties':{}})
        doc.saveas(self.path)
        self.session=EditorSession(load_dxf(self.path));self.settings(['R.domain'])
        self.options=MapOptions('T1',step_m=2)
        result=self.build()
        for xy in ((10,10),(30,6)):
            self.assertEqual(result.status_at_cell(*result.grid.locate(*xy)),'OUTSIDE')
        self.assertEqual(result.status_at_cell(*result.grid.locate(44,6)),'PASS')
        self.assertFalse(result.kernel.inspect(10,10)['inside_map_domain'])

    def test_no_zone_fail_not_fake_rectangle(self):
        s=deepcopy(self.session.check_settings);s['confirmed_absent']=['context.green_zone']
        self.session.set_check_settings(s)
        with self.assertRaises(EditorError):self.build()

    def test_mm_and_shift_same_results(self):
        baseline=self.build()
        path=make_demo(self.folder/'mm.dxf',millimetres=True,origin=(10000,-5000))
        self.session=EditorSession(load_dxf(path))
        result=self.build()
        self.assertEqual(baseline.combined,result.combined)
        self.assertEqual(result.kernel.inspect(10040,-4969)['status'],'FAIL')

    def test_data_export_and_run_encoding(self):
        result=self.build()
        data=result.to_dict()
        restored=json.loads(json.dumps(data,allow_nan=False))
        self.assertEqual(restored['point_semantics'],'cell_center')
        self.assertEqual(restored['row_order'],'y_ascending')
        self.assertEqual(sum(result.counts().values()),result.sampled_count)
        self.assertGreater(len(list(result.runs())),0)
        self.assertFalse(restored['auto_planting'])
        self.assertEqual(restored['combined'],list(result.combined))

if __name__=='__main__':unittest.main()
