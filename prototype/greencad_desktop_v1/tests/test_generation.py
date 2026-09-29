"""Generation contracts and independent geometric/transactional regression tests."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import asdict, replace
import json
import math
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

import ezdxf
from shapely.geometry import Point
from greencad_editor.catalog import ROOT
from greencad_editor.checks import (make_request, validate_plan, report_envelope, PointEvaluator,
                                   fingerprint, load_rules)
from greencad_editor.demo import make_demo
from greencad_editor.dxf_io import load_dxf, export_dxf
from greencad_editor.generation import (GenerationTarget, GenerationOptions, generate_plan, apply_generation,
    zone_choices, REQUIRED_RULES, ZONE_RULE_ID, MAX_NEW_PLACEMENTS)
from greencad_editor.metadata import encode_metadata
from greencad_editor.project_io import load_project, save_project
from greencad_editor.session import EditorSession
from greencad_editor.templates import new_template, profile_radius
from greencad_editor.errors import EditorError


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name)
        self.s = EditorSession(load_dxf(make_demo(self.path/'site.dxf')))
        for tid, radius in [('T1', 1.5), ('T2', 1.0), ('T3', .8), ('T4', .4)]:
            t = deepcopy(self.s.types[tid]); t.template = new_template()
            t.template['limits']['crown_radius_m'] = radius
            self.s.set_type(t)
    def tearDown(self): self.tmp.cleanup()
    def options(self, *pairs, step=2, zone='green_01'):
        return GenerationOptions(zone, tuple(GenerationTarget(t, n) for t,n in (pairs or [('T1', 4)])), step)
    def run_gen(self, options=None, **kwargs): return generate_plan(make_request(self.s), options or self.options(), **kwargs)
    def use(self, enabled=None, **site):
        settings = deepcopy(self.s.check_settings)
        if enabled is not None: settings['enabled'] = enabled
        settings['site'].update(site); self.s.set_check_settings(settings)
    def test_deterministic_coordinates_ids_and_input_unchanged(self):
        before = self.s.state(); source = self.s.drawing.source_bytes
        a = self.run_gen(self.options(('T1',6),('T2',5))); b = self.run_gen(self.options(('T1',6),('T2',5)))
        self.assertEqual(a.report['status'],'READY'); self.assertEqual([asdict(p) for p in a.placements],[asdict(p) for p in b.placements])
        self.assertEqual(self.s.state(),before); self.assertEqual(self.s.drawing.source_bytes,source)
        self.assertEqual(a.report['job_id'],b.report['job_id']); self.assertIsNone(self.s.validation_report)
    def test_all_types_counts_and_root_mode(self):
        t=deepcopy(self.s.types['T3']);t.template['root_protection']='REQUIRED';self.s.set_type(t)
        result=self.run_gen(self.options(('T1',4),('T2',5),('T3',6),('T4',7)))
        self.assertEqual([s['placed'] for s in result.report['statistics']],[4,5,6,7])
        self.assertTrue(all(p.root_protection for p in result.placements if p.type_id=='T3'))
        self.assertTrue(all(p.plant_id is None for p in result.placements))
    def test_manual_outside_selected_zone_retained_and_respected(self):
        manual=self.s.add('T1',20,10);self.s.change(manual.id,locked=True,label='ручная')
        before=asdict(self.s.placements[manual.id]);result=self.run_gen(self.options(('T1',10)))
        self.assertEqual(result.report['status'],'READY');apply_generation(self.s,result)
        self.assertEqual(asdict(self.s.placements[manual.id]),before)
        self.assertTrue(all(math.hypot(p.x-manual.x,p.y-manual.y)>=3 for p in result.placements))
        self.assertEqual(len(self.s.placements),11)
    def test_actual_pair_constraints_independent_of_engine(self):
        t=deepcopy(self.s.types['T2']);t.template['minimum_distances_m']['R.spacing']=5.5;self.s.set_type(t)
        result=self.run_gen(self.options(('T1',10),('T2',10),step=1))
        pts=list(result.placements)
        for i,a in enumerate(pts):
            for b in pts[i+1:]:
                required=max(3,5.5 if 'T2' in (a.type_id,b.type_id) else 0,
                             profile_radius(self.s.types[a.type_id])+profile_radius(self.s.types[b.type_id]))
                self.assertGreaterEqual(math.hypot(a.x-b.x,a.y-b.y),required)
        for p in pts:
            r=profile_radius(self.s.types[p.type_id]);self.assertTrue(2+r<=p.x<=78-r and 2+r<=p.y<=58-r)
            self.assertGreaterEqual(abs(p.y-30),3)
    def test_later_larger_type_does_not_use_stale_spacing_maximum(self):
        t=deepcopy(self.s.types['T2']);t.template['minimum_distances_m']['R.spacing']=12;self.s.set_type(t)
        r=self.run_gen(self.options(('T1',5),('T2',5),('T3',10)))
        self.assertEqual(r.validation['mandatory_status'],'PASS')
        for a in r.placements:
            for b in r.placements:
                if a.id!=b.id and 'T2' in (a.type_id,b.type_id):self.assertGreaterEqual(math.hypot(a.x-b.x,a.y-b.y),12)
    def test_apply_one_undo_redo_preserves_original(self):
        self.s.add('T1',20,10);old=self.s.state();idx=self.s._index
        r=self.run_gen();new=apply_generation(self.s,r)
        self.assertEqual(new,4);self.assertEqual(self.s._index,idx+1);self.assertEqual(report_envelope(self.s)['state'],'CURRENT')
        accepted=self.s.state();self.s.undo();self.assertEqual(self.s.state(),old)
        self.s.redo();self.assertEqual(self.s.state(),accepted)
    def test_force_core_checks_only_applied_on_accept(self):
        self.use([]);before=self.s.state();r=self.run_gen()
        self.assertEqual(self.s.state(),before);self.assertEqual(r.report['added_rules'],list(REQUIRED_RULES))
        apply_generation(self.s,r);self.assertTrue(set(REQUIRED_RULES)<=set(self.s.check_settings['enabled']))
        self.s.undo();self.assertEqual(self.s.check_settings['enabled'],[])
    def test_stale_preview_refuses_apply(self):
        r=self.run_gen();self.s.add('T1',60,50)
        before=self.s.state()
        with self.assertRaises(EditorError):apply_generation(self.s,r)
        self.assertEqual(self.s.state(),before)
    def test_altered_preview_refuses_apply(self):
        r=self.run_gen();r.placements[0].x=40;r.placements[0].y=30
        with self.assertRaises(EditorError):apply_generation(self.s,r)
        self.assertFalse(self.s.placements)
    def test_double_apply_refuses_duplicates(self):
        r=self.run_gen();apply_generation(self.s,r)
        with self.assertRaises(EditorError):apply_generation(self.s,r)
        self.assertEqual(len(self.s.placements),4)
    def test_cancel_immediately_and_during_progress_does_not_modify(self):
        before=self.s.state();cancel=Event();cancel.set()
        self.assertIsNone(self.run_gen(cancel=cancel));cancel.clear()
        def progress(stage,n,total):
            if stage.startswith('Расстановка'):cancel.set()
        self.assertIsNone(self.run_gen(cancel=cancel,progress=progress));self.assertEqual(before,self.s.state())
    def test_cancel_final_verification_discards_all(self):
        cancel=Event()
        def progress(stage,n,total):
            if stage=='Полная проверка результата' and n>0:cancel.set()
        self.assertIsNone(self.run_gen(cancel=cancel,progress=progress));self.assertFalse(self.s.placements)
    def test_invalid_manual_plant_blocks_whole_proposal(self):
        self.s.add('T1',40,30);old=self.s.state();r=self.run_gen()
        self.assertEqual(r.report['status'],'BASELINE_BLOCKED');self.assertEqual(r.validation['mandatory_status'],'FAIL')
        self.assertFalse(r.can_apply);self.assertEqual(old,self.s.state())
    def test_unknown_manual_requirements_block(self):
        self.s.add('T1',20,10);self.use(['R.domain','R.soil.drained'],soil_drained=True)
        r=self.run_gen();self.assertEqual(r.report['status'],'BASELINE_BLOCKED')
        self.assertEqual(r.validation['mandatory_status'],'UNKNOWN')
    def test_unknown_new_type_not_silently_filled(self):
        self.use(['R.domain','R.moisture_range']);r=self.run_gen(self.options(step=10))
        self.assertEqual(r.report['status'],'EMPTY');self.assertFalse(r.can_apply)
        self.assertGreater(r.report['statistics'][0]['outcomes'].get('UNKNOWN',0),0)
        self.assertTrue(any(c['rule_id']=='R.moisture_range' for x in r.report['rejections'] for c in x['checks']))
    def test_one_unknown_type_does_not_block_other_complete_template(self):
        self.use(['R.domain','R.moisture_range'],A1=.5)
        t=deepcopy(self.s.types['T2']);t.template['moisture_range']=[.2,.8];self.s.set_type(t)
        r=self.run_gen(self.options(('T1',3),('T2',3),step=8))
        self.assertEqual(r.report['status'],'PARTIAL');self.assertEqual([s['placed'] for s in r.report['statistics']],[0,3])
        self.assertTrue(r.can_apply)
    def test_advice_unknown_does_not_become_hard_ban(self):
        self.use(['R.domain','R.territory']);r=self.run_gen()
        self.assertTrue(r.can_apply);self.assertEqual(r.validation['mandatory_status'],'PASS')
        self.assertEqual(r.validation['status'],'UNKNOWN')
    def test_unknown_rule_refuses_preparation(self):
        self.use(['unregistered'])
        with self.assertRaises(EditorError):self.run_gen()
    def test_not_implemented_or_invalid_scope_does_not_pass(self):
        rules=load_rules(self.s.reference)
        for r in rules:
            if r['id']=='project.gas':r['operator']='missing'
        # Test injected invalid runtime definitions; originals remain read-only.
        with patch('greencad_editor.generation.load_rules',return_value=rules):
            # Effective rules need also feed the evaluator, otherwise a different
            # source would be used. This path is checked through source profile below.
            rules[2]['evaluation_scope']='UNRECOGNIZED'
            with self.assertRaises(EditorError):self.run_gen()
    def test_unimplemented_active_handler_gives_unknown_not_permission(self):
        rules=load_rules(self.s.reference)
        for r in rules:
            if r['id']=='project.gas':r['operator']='not_implemented'
        with patch('greencad_editor.generation.load_rules',return_value=rules):
            r=self.run_gen(self.options(step=12))
        self.assertEqual(r.report['status'],'EMPTY')
        self.assertTrue(any(c['rule_id']=='project.gas' and c['status']=='UNKNOWN'
                            for x in r.report['rejections'] for c in x['checks']))
    def test_fresh_validation_catches_broken_incremental_spatial_index(self):
        # Deliberately simulate a bug in acceptance indexing. The final validator
        # must independently rediscover the conflicts and refuse application.
        def broken_append(ev,p):ev.request.placements.append(deepcopy(p))
        with patch.object(PointEvaluator,'append_placement',broken_append):
            r=self.run_gen(self.options(('T1',5),step=1))
        self.assertEqual(r.report['status'],'VALIDATION_BLOCKED')
        self.assertEqual(r.validation['mandatory_status'],'FAIL')
        self.assertFalse(r.can_apply)
        with self.assertRaises(EditorError):apply_generation(self.s,r)
        self.assertFalse(self.s.placements)
    def test_guard_rule_wrong_scope_is_refused(self):
        rules=load_rules(self.s.reference)
        for r in rules:
            if r['id']=='R.spacing':r['evaluation_scope']='SITE_POINT'
        with patch('greencad_editor.generation.load_rules',return_value=rules):
            with self.assertRaises(EditorError):self.run_gen()
    def test_reserve_not_symbol_radius(self):
        a=self.run_gen();t=deepcopy(self.s.types['T1']);t.radius_m=25;self.s.set_type(t);b=self.run_gen()
        self.assertEqual([(p.x,p.y) for p in a.placements],[(p.x,p.y) for p in b.placements])
    def test_missing_type_profile_and_bad_counts(self):
        for options in [self.options(('T5',5)),self.options(('T1',True)),self.options(('T1',0)),
                        self.options(('T1',-1)),self.options(('T1',MAX_NEW_PLACEMENTS+1)),self.options(('missing',4)),
                        self.options(('T1',1),('T1',2))]:
            with self.assertRaises(EditorError):self.run_gen(options)
    def test_invalid_step_and_grid_budget(self):
        for step in [0,-1,float('nan'),float('inf'),1e-12,True]:
            with self.assertRaises(EditorError):self.run_gen(self.options(step=step))
    def test_empty_plan_and_exhaustion_are_not_infeasibility_proofs(self):
        t=deepcopy(self.s.types['T1']);t.template['limits']['crown_radius_m']=100;self.s.set_type(t)
        r=self.run_gen(self.options(step=10));self.assertEqual(r.report['status'],'EMPTY')
        self.assertIn('не доказательство',r.report['message']);self.assertFalse(r.can_apply)
    def test_large_step_partial_remains_valid(self):
        r=self.run_gen(self.options(('T1',100),step=20))
        self.assertEqual(r.report['status'],'PARTIAL');self.assertTrue(r.can_apply)
        self.assertLess(len(r.placements),100)
    def test_every_examined_rejection_has_reason(self):
        r=self.run_gen(self.options(('T1',15),step=1))
        self.assertEqual(len(r.report['rejections']),sum(s['examined']-s['placed'] for s in r.report['statistics']))
        self.assertTrue(all(row['checks'] for row in r.report['rejections']))
        self.assertTrue(all(any(c['rule_id']==ZONE_RULE_ID for c in row['checks']) for row in r.validation['placements']))
    def test_zero_spacing_and_no_reserve_refused(self):
        t=deepcopy(self.s.types['T1']);t.template['limits']={};self.s.set_type(t)
        self.s.check_settings['thresholds_m']['R.spacing']=0
        with self.assertRaises(EditorError):self.run_gen()
    def test_subset_zone_really_limits_reserve_not_union(self):
        path=self.path/'two.dxf';doc=ezdxf.readfile(self.path/'site.dxf');msp=doc.modelspace()
        # Overlapping larger domain still exists; this small zone must constrain us.
        e=msp.add_lwpolyline([(10,10),(20,10),(20,20),(10,20)],close=True,dxfattribs={'layer':'GC_GREEN_ZONE'})
        encode_metadata(e,{'role':'asset','id':'small','type_id':'context.green_zone','geometry_role':'AREA','properties':{}})
        doc.saveas(path);old=self.s.state();self.s=EditorSession(load_dxf(path));self.s.restore(old,mark_saved=True)
        self.s.add('T1',60,45)  # outside selected zone is allowed in the unchanged plan
        r=self.run_gen(self.options(('T1',20),step=1,zone='small'))
        self.assertTrue(r.can_apply)
        for p in r.placements:self.assertTrue(11.5<=p.x<=18.5 and 11.5<=p.y<=18.5)
    def test_hole_and_multicomponent_zone(self):
        doc=ezdxf.readfile(self.path/'site.dxf');msp=doc.modelspace()
        for e in list(msp):
            if e.dxf.layer=='GC_GREEN_ZONE':msp.delete_entity(e)
        e=msp.add_hatch(dxfattribs={'layer':'GC_GREEN_ZONE'})
        for ring in [[(3,3),(25,3),(25,23),(3,23)],[(8,8),(18,8),(18,18),(8,18)],[(50,40),(75,40),(75,55),(50,55)]]:
            e.paths.add_polyline_path(ring,is_closed=True)
        encode_metadata(e,{'role':'asset','id':'green_01','type_id':'context.green_zone','geometry_role':'AREA','properties':{}})
        path=self.path/'holes.dxf';doc.saveas(path);state=self.s.state();self.s=EditorSession(load_dxf(path));self.s.restore(state)
        r=self.run_gen(self.options(('T1',100),step=2));self.assertTrue(r.can_apply)
        self.assertTrue(any(p.x>50 for p in r.placements))
        self.assertTrue(all(not 8<=p.x<=18 or not 8<=p.y<=18 for p in r.placements))
    def test_mm_and_translation_invariance(self):
        a=self.run_gen();state=self.s.state();origin=(10000,20000)
        self.s=EditorSession(load_dxf(make_demo(self.path/'mm.dxf',millimetres=True,origin=origin)));self.s.restore(state)
        b=self.run_gen()
        self.assertEqual([(p.x,p.y) for p in a.placements],[(p.x-origin[0],p.y-origin[1]) for p in b.placements])
    def test_source_unknown_geometry_never_becomes_permission(self):
        doc=ezdxf.readfile(self.path/'site.dxf');doc.modelspace().add_line((0,0),(1,1),dxfattribs={'layer':'unknown'})
        path=self.path/'bad.dxf';doc.saveas(path);state=self.s.state();self.s=EditorSession(load_dxf(path));self.s.restore(state)
        with self.assertRaises(ValueError):self.run_gen()
    def test_missing_zone_is_error(self):
        with self.assertRaises(ValueError):self.run_gen(self.options(zone='does_not_exist'))
    def test_saved_gcp_dxf_roundtrip_and_recheck(self):
        before=self.s.drawing.source_bytes;r=self.run_gen();apply_generation(self.s,r)
        gcp=self.path/'result.gcp';save_project(self.s,gcp);s,_=load_project(gcp)
        self.assertEqual(s.state(),self.s.state());self.assertEqual(s.drawing.source_bytes,before)
        out=self.path/'out.dxf';export_dxf(s.drawing,list(s.placements.values()),s.types,out)
        reread=EditorSession(load_dxf(out));self.assertEqual(len(reread.placements),4)
        for p in reread.placements.values():self.assertEqual(p.properties['generation']['zone_id'],'green_01')
        self.assertEqual(validate_plan(make_request(s))['status'],'PASS')
    def test_cli_success_and_existing_output_guard(self):
        from tools.generate_layout import main
        src=self.path/'src.gcp';save_project(self.s,src);out=self.path/'generated.dxf';project=self.path/'generated.gcp'
        args=[str(src),'--zone','green_01','--type','T1:4','--output',str(out),'--project-output',str(project)]
        with patch('builtins.print'):
            self.assertEqual(main(args),0);self.assertEqual(main(args),4)
        self.assertTrue(out.exists());self.assertTrue(project.exists());self.assertTrue(out.with_suffix('.generation.json').exists())
        data=json.loads(out.with_suffix('.checks.json').read_text());self.assertEqual(data['state'],'CURRENT')
    def test_cli_empty_only_report_no_unverified_dxf(self):
        from tools.generate_layout import main
        self.use(['R.moisture_range']);src=self.path/'src.gcp';save_project(self.s,src);out=self.path/'none.dxf'
        with patch('builtins.print'):self.assertEqual(main([str(src),'--zone','green_01','--type','T1:4','--step','10','--output',str(out)]),3)
        self.assertFalse(out.exists());self.assertTrue(out.with_suffix('.generation.json').exists())
    def test_change_target_order_changes_deterministic_layout(self):
        a=self.run_gen(self.options(('T1',5),('T2',5)));b=self.run_gen(self.options(('T2',5),('T1',5)))
        self.assertEqual(a.placements[0].type_id,'T1');self.assertEqual(b.placements[0].type_id,'T2')
        self.assertNotEqual(a.report['job_id'],b.report['job_id'])
    def test_options_serialization_and_unknown_keys(self):
        opts=self.options(('T1',5),('T2',4));self.assertEqual(GenerationOptions.from_dict(json.loads(json.dumps(asdict(opts)))),opts)
        with self.assertRaises(EditorError):GenerationOptions.from_dict({'unrecognized':True})
    def test_reference_source_snapshot_is_not_modified(self):
        before=self.s.reference.to_bytes();self.run_gen();self.assertEqual(before,self.s.reference.to_bytes())

if __name__=='__main__':unittest.main()
