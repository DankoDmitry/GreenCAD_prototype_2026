"""Reproducible free sampling: constraints remain the existing evaluator's job."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import asdict, replace
import json
import math
import random
from threading import Event
import unittest
from unittest.mock import patch

from shapely.geometry import Polygon, Point
from shapely.prepared import prep

import test_generation as base
from greencad_editor.admissibility import GridSpec
from greencad_editor.checks import make_request, validate_plan
from greencad_editor.dxf_io import load_dxf, export_dxf
from greencad_editor.errors import EditorError
from greencad_editor.generation import (GenerationOptions, GenerationTarget, generate_plan,
    apply_generation, search_candidates, MAX_SEED)
from greencad_editor.project_io import save_project, load_project
from greencad_editor.session import EditorSession
from greencad_editor.templates import profile_radius


def positions(result):
    return [(p.type_id, p.x, p.y) for p in result.placements]


class NaturalGenerationTests(unittest.TestCase):
    setUp = base.GenerationTests.setUp
    tearDown = base.GenerationTests.tearDown
    use = base.GenerationTests.use

    def options(self, count=20, seed=1, step=2, layout='natural'):
        return GenerationOptions('green_01', (GenerationTarget('T1', count),), step, layout, seed)

    def run_gen(self, options=None, **kwargs):
        return generate_plan(make_request(self.s), options or self.options(), **kwargs)

    def test_legacy_json_uses_grid_and_new_json_roundtrips(self):
        data={'zone_id':'green_01','targets':[{'type_id':'T1','max_new':4}],'step_m':2}
        old=GenerationOptions.from_dict(data)
        self.assertEqual(old.layout,'grid')
        new=self.options()
        self.assertEqual(GenerationOptions.from_dict(json.loads(json.dumps(asdict(new)))),new)
        self.assertEqual(positions(self.run_gen(old)),positions(self.run_gen(self.options(count=4,layout='grid'))))
        self.assertTrue(all((p.x-2)%2 == 1 and (p.y-2)%2 == 1 for p in self.run_gen(old).placements))

    def test_bad_seed_and_mode_are_errors_not_random_defaults(self):
        for seed in (True,False,-1,MAX_SEED+1,2.5,'10',None):
            with self.subTest(seed=seed), self.assertRaises(EditorError):
                self.run_gen(self.options(seed=seed))
        for mode in ('jitter','',None,[]):
            with self.subTest(mode=mode), self.assertRaises(EditorError):
                self.run_gen(self.options(layout=mode))

    def test_same_seed_same_geometry_ids_and_no_input_or_global_rng_changes(self):
        state=self.s.state(); random_state=random.getstate()
        a=self.run_gen();b=self.run_gen()
        self.assertEqual([asdict(p) for p in a.placements],[asdict(p) for p in b.placements])
        self.assertEqual(a.report['job_id'],b.report['job_id'])
        self.assertEqual(random.getstate(),random_state)
        self.assertEqual(self.s.state(),state)
        self.assertTrue(a.can_apply)

    def test_different_seed_changes_variant(self):
        a=self.run_gen(self.options(seed=1));b=self.run_gen(self.options(seed=2))
        self.assertNotEqual(positions(a),positions(b))
        self.assertNotEqual(a.report['job_id'],b.report['job_id'])
        self.assertEqual(a.report['sampling']['seed'],1)
        self.assertEqual(b.report['sampling']['seed'],2)

    def test_free_mode_has_no_common_grid_rows_and_spreads_across_site(self):
        r=self.run_gen(self.options(count=40,step=1))
        pts=r.placements
        self.assertEqual(len(pts),40)
        self.assertEqual(len({round(p.y,7) for p in pts}),len(pts))
        self.assertTrue(any(abs(p.x%1-.5)>.01 for p in pts))
        # A regression for this fixed seed, not a general statistical promise.
        quadrants={(p.x>=40,p.y>=30) for p in pts}
        self.assertEqual(len(quadrants),4)

    def test_100_is_legal_not_an_accidental_loophole(self):
        r=self.run_gen(self.options(count=100,step=1))
        self.assertGreater(len(r.placements),20)
        self.assertLessEqual(len(r.placements),100)
        self.assertEqual(r.validation['mandatory_status'],'PASS')

    def test_all_constraints_and_manual_placements_independently_checked(self):
        manual=self.s.add('T1',20,10);self.s.change(manual.id,locked=True)
        old=asdict(self.s.placements[manual.id])
        t=deepcopy(self.s.types['T2']);t.template['minimum_distances_m']['R.spacing']=5.5
        self.s.set_type(t)
        opts=GenerationOptions('green_01',(GenerationTarget('T1',15),GenerationTarget('T2',12)),1,'natural',9)
        r=self.run_gen(opts)
        self.assertEqual(r.validation['mandatory_status'],'PASS')
        all_pts=list(self.s.placements.values())+list(r.placements)
        for p in r.placements:
            radius=profile_radius(self.s.types[p.type_id])
            self.assertTrue(2+radius<=p.x<=78-radius)
            self.assertTrue(2+radius<=p.y<=58-radius)
            self.assertGreaterEqual(abs(p.y-30),3)
        for i,a in enumerate(all_pts):
            for b in all_pts[i+1:]:
                required=max(3,5.5 if 'T2' in (a.type_id,b.type_id) else 0,
                    profile_radius(self.s.types[a.type_id])+profile_radius(self.s.types[b.type_id]))
                self.assertGreaterEqual(math.hypot(a.x-b.x,a.y-b.y),required)
        self.assertEqual(asdict(self.s.placements[manual.id]),old)

    def test_holes_bounds_and_last_short_cell_sampling(self):
        shape=Polygon([(0,0),(5.3,0),(5.3,4.7),(0,4.7)],holes=[[(1,1),(2,1),(2,2),(1,2)]])
        grid=GridSpec.create(shape.bounds,1.0)
        records=search_candidates(grid,prep(shape),self.options(),lambda *a:None,lambda:None)
        self.assertLessEqual(len(records),grid.count)
        self.assertEqual(len({i for i,_,_ in records}),len(records))
        for i,x,y in records:
            self.assertTrue(shape.covers(Point(x,y)))
            a,b,c,d=grid.cell_bounds(i%grid.nx,i//grid.nx)
            self.assertTrue(a<=x<=c and b<=y<=d)

    def test_cancel_leaves_no_partial_changes(self):
        event=Event();before=self.s.state()
        def progress(stage,done,total):
            if stage=='Подготовка пробных позиций':event.set()
        self.assertIsNone(self.run_gen(cancel=event,progress=progress))
        self.assertEqual(self.s.state(),before)

    def test_unknown_condition_cannot_become_accepted(self):
        self.use(['R.domain','R.moisture_range'])
        r=self.run_gen(self.options(step=8))
        self.assertEqual(r.report['status'],'EMPTY')
        self.assertFalse(r.can_apply)
        self.assertTrue(any(c['status']=='UNKNOWN' for row in r.report['rejections'] for c in row['checks']))

    def test_apply_undo_save_export_keep_seed_and_exact_coordinates(self):
        before=self.s.state();r=self.run_gen(self.options(count=6,seed=314))
        apply_generation(self.s,r);state=self.s.state()
        self.s.undo();self.assertEqual(self.s.state(),before)
        self.s.redo();self.assertEqual(self.s.state(),state)
        save_project(self.s,self.path/'test.gcp');opened,_=load_project(self.path/'test.gcp')
        self.assertEqual(opened.state(),state)
        export_dxf(opened.drawing,list(opened.placements.values()),opened.types,self.path/'new.dxf')
        reread=EditorSession(load_dxf(self.path/'new.dxf'))
        for p in r.placements:
            q=reread.placements[p.id]
            self.assertEqual(q.properties['generation']['seed'],314)
            self.assertEqual(q.properties['generation']['layout'],'natural')
            self.assertAlmostEqual(q.x,p.x,places=9);self.assertAlmostEqual(q.y,p.y,places=9)
        self.assertEqual(validate_plan(make_request(opened))['status'],'PASS')

    def test_grid_does_not_use_seed_to_change_coordinates(self):
        self.assertEqual(positions(self.run_gen(self.options(layout='grid',seed=1))),
                         positions(self.run_gen(self.options(layout='grid',seed=2))))

    def test_cli_natural_outputs_reproducible_metadata(self):
        from tools.generate_layout import main
        save_project(self.s,self.path/'input.gcp')
        out=self.path/'out.dxf'
        args=[str(self.path/'input.gcp'),'--zone','green_01','--type','T1:4',
              '--layout','natural','--seed','42','--output',str(out)]
        with patch('builtins.print'):self.assertEqual(main(args),0)
        data=json.loads(out.with_suffix('.generation.json').read_text())
        self.assertEqual(data['options']['layout'],'natural')
        self.assertEqual(data['options']['seed'],42)
        self.assertEqual(data['validation']['mandatory_status'],'PASS')

if __name__=='__main__':unittest.main()
