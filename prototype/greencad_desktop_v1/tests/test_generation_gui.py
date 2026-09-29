"""Real Tk events for preview/apply, no modal sleeps or untested screenshot mockups."""
from __future__ import annotations
from copy import deepcopy
import gc
import json
import os
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from greencad_editor.ui import EditorApp
from greencad_editor.session import EditorSession
from greencad_editor.demo import make_demo
from greencad_editor.dxf_io import load_dxf
from greencad_editor.templates import new_template
from greencad_editor.generation import GenerationTarget, GenerationOptions
from greencad_editor.generation_ui import GenerationDialog
from greencad_editor.admissibility import MapOptions
from greencad_editor.project_io import load_project, save_project


@unittest.skipUnless(os.environ.get('DISPLAY') or os.name=='nt','Требуется Tk/Xvfb')
class GenerationGuiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.folder=Path(self.tmp.name)
        self.root=tk.Tk();self.app=EditorApp(self.root);self.errors=[]
        self.root.report_callback_exception=lambda kind,value,tb:self.errors.append(value)
        self.error_patch=patch('greencad_editor.generation_ui.messagebox.showerror',side_effect=lambda *a,**k:self.errors.append(a))
        self.error_patch.start()
        s=EditorSession(load_dxf(make_demo(self.folder/'site.dxf')))
        for tid,r in [('T1',1.5),('T2',.8)]:
            t=deepcopy(s.types[tid]);t.template=new_template();t.template['limits']['crown_radius_m']=r;s.set_type(t)
        self.app.adopt(s);self.ctl=self.app.generation_controller;self.pump()
    def tearDown(self):
        self.app.executor.shutdown(wait=True);self.ctl.shutdown();self.app.admissibility_controller.shutdown()
        for job in self.root.tk.call('after','info'):
            try:self.root.tk.call('after','cancel',job)
            except tk.TclError:pass
        self.root.destroy();self.tmp.cleanup();self.error_patch.stop()
        self.app=self.root=self.ctl=None;gc.collect()
        self.assertEqual(self.errors,[])
    def pump(self,duration=.06):
        until=time.monotonic()+duration
        while time.monotonic()<until:self.root.update();time.sleep(.002)
    def wait(self):
        start=time.monotonic()
        while self.app.busy and time.monotonic()-start<25:self.pump()
        self.assertFalse(self.app.busy);self.pump(.15)
    def click(self,w):
        w.event_generate('<Enter>');x=w.winfo_width()//2;y=w.winfo_height()//2
        w.event_generate('<ButtonPress-1>',x=x,y=y);w.event_generate('<ButtonRelease-1>',x=x,y=y);self.pump()
    def build(self,count=5):
        opts=GenerationOptions('green_01',(GenerationTarget('T1',count),GenerationTarget('T2',count)),2)
        self.ctl.start(opts);self.wait();self.assertEqual(self.ctl.state(),'CURRENT')
    def test_real_dialog_build_and_ghosts_do_not_edit_project(self):
        before=self.app.session.state();self.click(self.ctl.build_button);d=self.ctl.options_window
        self.assertIsInstance(d,GenerationDialog);d.cap.set('4');d.step.set('2')
        self.click(d.build_button);self.wait()
        self.assertEqual(self.ctl.result.options.targets[0].max_new,4)
        self.assertEqual(self.app.session.state(),before)
        self.assertTrue(self.app.canvas.find_withtag('generation_preview'))
        self.assertEqual(str(self.ctl.apply_button.cget('state')),'normal')
    def test_real_apply_button_one_undo_and_redo(self):
        self.build();self.click(self.ctl.apply_button)
        self.assertEqual(len(self.app.session.placements),10);self.assertFalse(self.app.canvas.find_withtag('generation_preview'))
        self.assertEqual(self.ctl.state(),'APPLIED')
        self.app.undo();self.pump();self.assertFalse(self.app.session.placements)
        self.assertFalse(self.app.canvas.find_withtag('generation_preview'))
        self.app.redo();self.pump();self.assertEqual(len(self.app.session.placements),10)
    def test_preview_discard_is_not_undo_or_delete(self):
        self.app.session.add('T1',20,10);self.app.on_change();before=self.app.session.state()
        self.build();self.click(self.ctl.discard_button)
        self.assertEqual(self.app.session.state(),before);self.assertFalse(self.app.canvas.find_withtag('generation_preview'))
    def test_preview_stale_on_manual_changes(self):
        self.build();self.app.session.add('T1',60,45);self.app.on_change();self.pump()
        self.assertEqual(self.ctl.state(),'STALE');self.assertIn('УСТАРЕЛ',self.ctl.status.get())
        self.assertEqual(str(self.ctl.apply_button.cget('state')),'disabled');self.assertFalse(self.app.canvas.find_withtag('generation_preview'))
    def test_preview_stale_on_rule_change(self):
        self.build();settings=deepcopy(self.app.session.check_settings);settings['thresholds_m']['project.gas']=9
        self.app.session.set_check_settings(settings);self.app.on_change();self.pump()
        self.assertEqual(self.ctl.state(),'STALE')
    def test_zoom_hidden_layers_do_not_invalidate(self):
        self.build();result=self.ctl.result;self.app.canvas.zoom(1.2);self.app.canvas.hidden_layers.add('GC_GAS_AXIS');self.pump()
        self.assertIs(result,self.ctl.result);self.assertEqual(self.ctl.state(),'CURRENT')
        self.assertTrue(self.app.canvas.find_withtag('generation_preview'))
    def test_cancel_never_adds_partial(self):
        self.ctl.start(GenerationOptions('green_01',(GenerationTarget('T1',200),),.7));self.ctl.stop_button.invoke();self.wait()
        self.assertIsNone(self.ctl.result);self.assertFalse(self.app.session.placements)
        self.assertIn('отменена',self.ctl.status.get());self.assertFalse(self.ctl.running)
    def test_unknown_parameter_yields_empty_report(self):
        settings=deepcopy(self.app.session.check_settings);settings['enabled'].append('R.moisture_range')
        self.app.session.set_check_settings(settings);self.app.on_change()
        self.ctl.start(GenerationOptions('green_01',(GenerationTarget('T1',3),),10));self.wait()
        self.assertEqual(self.ctl.result.report['status'],'EMPTY');self.assertEqual(str(self.ctl.apply_button.cget('state')),'disabled')
        self.assertTrue(self.ctl.review_window.winfo_exists())
        self.assertIn('R.moisture_range',self.ctl.review_window.pages['rejected'].get('1.0','end'))
    def test_existing_violations_shown_not_repaired(self):
        p=self.app.session.add('T1',40,30);self.app.on_change();self.build()
        self.assertEqual(self.ctl.result.report['status'],'BASELINE_BLOCKED')
        self.assertEqual(self.app.session.placements[p.id].y,30)
        self.assertIn('газопровода',self.ctl.review_window.pages['checks'].get('1.0','end'))
    def test_type_order_and_toggle_real_events(self):
        self.click(self.ctl.build_button);d=self.ctl.options_window
        d.tree.selection_set('T2');d.select_row();self.click(d.up_button)
        self.assertEqual(d.order[0],'T2')
        x,y,w,h=d.tree.bbox('T1','#1')
        d.tree.event_generate('<ButtonPress-1>',x=x+10,y=y+h//2);d.tree.event_generate('<ButtonRelease-1>',x=x+10,y=y+h//2);self.pump()
        self.assertNotIn('T1',d.enabled);d.step.set('3');self.click(d.build_button);self.wait()
        self.assertEqual([t.type_id for t in self.ctl.result.options.targets],['T2'])
    def test_dialog_cancel_no_changes(self):
        old=self.app.session.state();self.click(self.ctl.build_button);d=self.ctl.options_window
        d.cap.set('100');self.click(d.cancel_button);self.assertEqual(self.app.session.state(),old)
    def test_report_export_and_saved_state_reopen(self):
        self.build();self.click(self.ctl.review_button);r=self.ctl.review_window
        path=self.folder/'generation.json'
        with patch('greencad_editor.generation_ui.filedialog.asksaveasfilename',return_value=str(path)):self.ctl.save()
        self.assertEqual(json.loads(path.read_text())['state'],'CURRENT')
        self.click(r.apply_button);saved=self.app.session.state();out=self.folder/'session.gcp';save_project(self.app.session,out)
        s,_=load_project(out);self.assertEqual(s.state(),saved);self.app.adopt(s);self.pump()
        self.assertEqual(self.ctl.state(),'NOT_BUILT');self.assertFalse(self.app.canvas.find_withtag('generation_preview'))
        self.assertEqual(len(s.placements),10)
    def test_map_and_preview_coexist_without_mutating_map(self):
        ctl=self.app.admissibility_controller;ctl.start(MapOptions('T1',step_m=4,template_only=True));self.wait();m=ctl.result
        self.build();self.assertIs(ctl.result,m)
        self.assertTrue(self.app.canvas.find_withtag('admissibility'));self.assertTrue(self.app.canvas.find_withtag('generation_preview'))
        self.click(self.ctl.apply_button)
        # R.spacing may be added: old map correctly becomes stale, not quietly reused.
        self.assertEqual(ctl.state(),'STALE')
    def test_footer_visible_at_large_scale_and_small_window(self):
        self.root.tk.call('tk','scaling',2.0);self.click(self.ctl.build_button);d=self.ctl.options_window
        for geometry in ('820x520','1000x680'):
            d.geometry(geometry);self.pump()
            for b in (d.build_button,d.cancel_button):
                self.assertTrue(b.winfo_ismapped());self.assertGreater(b.winfo_height(),1)
                self.assertLessEqual(b.winfo_rooty()+b.winfo_height(),d.winfo_rooty()+d.winfo_height())
                self.assertLessEqual(b.winfo_rootx()+b.winfo_width(),d.winfo_rootx()+d.winfo_width())
    def test_completed_error_returns_controls_to_idle(self):
        with patch('greencad_editor.generation_ui.generate_plan',side_effect=EditorErrorForTest('test-error')):
            self.ctl.start(GenerationOptions('green_01',(GenerationTarget('T1',1),),2));self.wait()
        self.assertFalse(self.ctl.running);self.assertFalse(self.app.busy);self.assertIsNone(self.ctl.result)
        self.assertEqual(str(self.ctl.apply_button.cget('state')),'disabled')
        self.assertEqual(len(self.errors),1);self.errors.clear()

class EditorErrorForTest(ValueError): pass

if __name__=='__main__':unittest.main()
