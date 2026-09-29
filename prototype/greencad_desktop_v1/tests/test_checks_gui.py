from __future__ import annotations
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch
from greencad_editor.demo import make_demo
from greencad_editor.dxf_io import load_dxf
from greencad_editor.session import EditorSession
from greencad_editor.ui import EditorApp
from greencad_editor.checks_ui import RulePicker
from greencad_editor.checks import default_settings


@unittest.skipUnless(sys.platform in ('win32','darwin') or os.environ.get('DISPLAY'),'Требуется Tk/Xvfb')
class CheckGuiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.folder=Path(self.tmp.name)
        self.path=make_demo(self.folder/'input.dxf')
        self.root=tk.Tk();self.app=EditorApp(self.root);self.errors=[]
        self.root.report_callback_exception=lambda typ,value,tb:self.errors.append(value)
        self.error_patch=patch('greencad_editor.ui.messagebox.showerror',side_effect=lambda *a,**k:self.errors.append(a))
        self.error_patch.start();self.root.update();self.app.adopt(EditorSession(load_dxf(self.path)));self.pump()

    def tearDown(self):
        self.app.executor.shutdown(wait=True);self.root.destroy();self.tmp.cleanup();self.error_patch.stop()
        self.app=None;self.root=None
        import gc;gc.collect()
        self.assertFalse(self.errors,repr(self.errors))

    def pump(self,n=5):
        for _ in range(n):self.root.update();time.sleep(.01)

    def wait(self):
        start=time.perf_counter()
        while self.app.busy and time.perf_counter()-start<10:self.pump(2)
        self.assertFalse(self.app.busy);self.pump()

    def add(self,x,y):
        p=self.app.session.add('T1',x,y);self.app.on_change();self.app.on_select(p.id,None);self.pump();return p

    def check(self):self.app.validation_controller.check_button.invoke();self.wait()

    def test_check_button_report_and_map(self):
        p=self.add(20,10);q=self.add(40,31);before=self.app.session.state()
        self.check();r=self.app.session.validation_report
        self.assertEqual(r['counts'],{'PASS':1,'FAIL':1})
        self.assertEqual(self.app.session.state(),before)
        self.assertTrue(self.app.canvas.find_withtag('validation:'+p.id))
        self.assertTrue(self.app.canvas.find_withtag('validation:'+q.id))
        self.assertIn('Нарушено',self.app.selection_caption.cget('text'))
        self.assertIn('gas_01',self.app.info.get('1.0','end'))
        self.assertTrue(self.app.validation_controller.window.winfo_exists())

    def test_picker_real_checkbox_and_settings_apply(self):
        self.add(40,31);self.check()
        ctl=self.app.validation_controller
        dlg=RulePicker(self.root,self.app.session,lambda settings:(self.app.session.set_check_settings(settings),self.app.on_change()))
        self.pump()
        rid='project.gas';box=dlg.tree.bbox(rid,'on');self.assertTrue(box)
        x,y,w,h=box
        dlg.tree.event_generate('<ButtonPress-1>',x=x+w//2,y=y+h//2)
        dlg.tree.event_generate('<ButtonRelease-1>',x=x+w//2,y=y+h//2);self.pump()
        self.assertNotIn(rid,dlg.selected)
        dlg.accept();self.pump();self.assertEqual(ctl.report_state(),'STALE')
        self.assertFalse(self.app.canvas.find_withtag('validation:'+self.app.session.selected_id))
        self.check();self.assertEqual(self.app.session.validation_report['status'],'PASS')

    def test_picker_range_and_a1(self):
        p=self.add(20,10)
        dlg=RulePicker(self.root,self.app.session,self.app.session.set_check_settings);self.pump()
        dlg.selected={'R.moisture_range'};dlg.a1.set('0,5');dlg.lo.set('0,2');dlg.hi.set('0,4')
        dlg.accept();self.pump();self.app.on_change();self.check()
        self.assertEqual(self.app.session.validation_report['status'],'FAIL')
        self.assertEqual(self.app.session.check_settings['site']['A1'],.5)
        self.assertEqual(self.app.session.check_settings['type_ranges']['T1'],[.2,.4])

    def test_move_stale_and_view_not_stale(self):
        p=self.add(20,10);self.check();ctl=self.app.validation_controller
        self.app.canvas.zoom(1.3);self.app.canvas.hidden_layers.add('GC_GAS_AXIS');self.app.canvas.request_render();self.pump()
        self.assertEqual(ctl.report_state(),'CURRENT')
        self.app.session.move(p.id,40,31);self.app.on_change();self.app.on_select(p.id,None);self.pump()
        self.assertEqual(ctl.report_state(),'STALE');self.assertFalse(self.app.canvas.find_withtag('validation:'+p.id))
        self.assertIn('УСТАРЕВШИЙ',self.app.info.get('1.0','end'))
        self.check();self.assertEqual(self.app.session.validation_report['status'],'FAIL')

    def test_report_select_filter(self):
        p=self.add(20,10);q=self.add(40,31);self.check()
        dlg=self.app.validation_controller.window
        dlg.filter.set('Нарушено');dlg.refresh();self.pump()
        self.assertEqual(dlg.tree.get_children(),(q.id,))
        dlg.tree.selection_set(q.id);self.pump()
        self.assertEqual(self.app.session.selected_id,q.id)
        self.assertIn('Фактически: 1.0',dlg.text.get('1.0','end'))

    def test_export_sidecar_current_and_stale(self):
        p=self.add(20,10);self.check()
        for state in ('CURRENT','STALE'):
            if state=='STALE':self.app.session.move(p.id,40,31);self.app.on_change()
            path=self.folder/(state+'.dxf')
            with patch('greencad_editor.ui.filedialog.asksaveasfilename',return_value=str(path)),patch('greencad_editor.ui.messagebox.showinfo'):
                self.app.export();self.wait()
            data=json.loads(path.with_suffix('.checks.json').read_text())
            self.assertEqual(data['state'],state);self.assertTrue(data['output_dxf']['sha256'])
            self.assertEqual(len(load_dxf(path).placements),1)

    def test_save_settings_reopen_requires_check(self):
        self.add(20,10);settings=deepcopy(self.app.session.check_settings);settings['enabled']=['R.domain'];self.app.session.set_check_settings(settings)
        self.check();path=self.folder/'session.gcp'
        with patch('greencad_editor.ui.filedialog.asksaveasfilename',return_value=str(path)):self.assertTrue(self.app.save())
        self.app.open_path(path,confirm=False);self.wait()
        self.assertEqual(self.app.session.check_settings['enabled'],['R.domain'])
        self.assertEqual(self.app.validation_controller.report_state(),'NOT_CHECKED')
        self.assertIsNone(self.app.session.validation_report)
        self.assertIn('ещё не проверен',self.app.validation_controller.window.text.get('1.0','end'))

    def test_unknown_no_false_green(self):
        p=self.add(20,10);settings=default_settings();settings['enabled']=['R.moisture_range'];self.app.session.set_check_settings(settings);self.app.on_change()
        self.check();self.assertEqual(self.app.session.validation_report['status'],'UNKNOWN')
        self.assertEqual(self.app.canvas.validation_statuses[p.id],'UNKNOWN')
        self.assertIn('Недостаточно данных',self.app.selection_caption.cget('text'))

if __name__=='__main__':unittest.main()
