from __future__ import annotations
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from greencad_editor.demo import make_demo
from greencad_editor.dxf_io import load_dxf
from greencad_editor.session import EditorSession
from greencad_editor.ui import EditorApp
from greencad_editor.checks import make_request,validate_plan
from greencad_editor.project_io import save_project,load_project
from test_reference import changed_value,PID

@unittest.skipUnless(os.environ.get('DISPLAY') or os.name=='nt','Нужен дисплей Tk/Xvfb')
class ReferenceGuiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.folder=Path(self.tmp.name)
        self.root=tk.Tk();self.app=EditorApp(self.root);self.errors=[]
        self.root.report_callback_exception=lambda typ,value,tb:self.errors.append(value)
        self.app.adopt(EditorSession(load_dxf(make_demo(self.folder/'site.dxf'))))
        self.pump()
    def tearDown(self):
        self.app.executor.shutdown(wait=True);self.app.admissibility_controller.shutdown()
        for job in self.root.tk.call('after','info'):
            try:self.root.tk.call('after','cancel',job)
            except tk.TclError:pass
        self.root.destroy();self.tmp.cleanup()
    def pump(self,t=.05):
        until=time.time()+t
        while time.time()<until:self.root.update();time.sleep(.005)
    def window(self):
        w=self.app.reference_controller.show();self.pump();return w
    def assert_clean(self):self.assertEqual(self.errors,[])

    def test_real_button_opens_readonly_catalog(self):
        b=self.app.reference_controller.button
        b.event_generate('<ButtonPress-1>',x=20,y=10);b.event_generate('<ButtonRelease-1>',x=20,y=10);self.pump()
        w=self.app.reference_controller.window;self.assertIsNotNone(w)
        self.assertEqual(len(w.plant_table.tree.get_children()),329)
        self.assertEqual(w.plant_info.cget('state'),'disabled')
        self.assert_clean()

    def test_search_and_source_card(self):
        w=self.window();w.plant_table.query.set('Тсуга');self.pump()
        self.assertIn(PID,w.plant_table.tree.get_children())
        w.plant_table.select(PID)
        w.property_table.select('plant.drained_soil_required:0');self.pump()
        text=w.plant_info.get('1.0','end')
        self.assertIn('V02205',text);self.assertIn('03_Значения',text);self.assertIn('E2210',text)
        self.assertIn('R.soil.drained',text);self.assert_clean()

    def test_numeric_constraints_show_test_and_project_override(self):
        s=self.app.session;settings=deepcopy(s.check_settings);settings['thresholds_m']['project.gas']=9;s.set_check_settings(settings)
        w=self.window();w.rule_table.select('project.gas');self.pump()
        text=w.rule_info.get('1.0','end');self.assertIn('9 м',text)
        self.assertIn('настройка текущего проекта',text);self.assertIn('не нормативный',text)
        self.assertIn('AXIS',text);self.assert_clean()

    def test_pending_rules_are_not_active(self):
        w=self.window();pending=[k for k in w.rule_table.tree.get_children() if k.startswith('pending:')]
        self.assertTrue(pending);w.rule_table.select(pending[0]);self.pump()
        self.assertIn('Сейчас не исполняется',w.rule_info.get('1.0','end'))
        self.assert_clean()

    def test_site_conditions_follow_same_session(self):
        w=self.window();settings=deepcopy(self.app.session.check_settings);settings['site']['A1']=.7
        self.app.session.set_check_settings(settings);self.app.on_change();self.pump()
        values=w.site_table.tree.item('parameter:A1','values');self.assertIn('0.7',values)
        self.assertEqual(w.bundle.id,self.app.session.reference.id);self.assert_clean()

    def test_view_does_not_change_project(self):
        before=self.app.session.state();w=self.window()
        for tab in (w.plants_tab,w.rules_tab,w.site_tab,w.sources_tab):w.tabs.select(tab);self.pump()
        self.assertEqual(before,self.app.session.state())
        self.assert_clean()

    def test_update_cancel_preserves_snapshot(self):
        w=self.window();old=self.app.session.reference.id
        with patch('greencad_editor.reference_ui.messagebox.askyesno',return_value=False):
            self.assertFalse(self.app.reference_controller.apply_bundle(changed_value(w.bundle)))
        self.assertEqual(self.app.session.reference.id,old);self.assert_clean()

    def test_update_adopt_undo_and_saved_snapshot(self):
        s=self.app.session;p=s.add('T1',20,10);s.change(p.id,plant_id=PID)
        settings=deepcopy(s.check_settings);settings['enabled']=['R.domain','R.soil.drained'];settings['site']['soil_drained']=False;s.set_check_settings(settings)
        s.validation_report=validate_plan(make_request(s));self.assertEqual(s.validation_report['status'],'FAIL')
        w=self.window();old=w.bundle.id
        with patch('greencad_editor.reference_ui.messagebox.askyesno',return_value=True):self.assertTrue(self.app.reference_controller.apply_bundle(changed_value(w.bundle)))
        self.pump();self.assertNotEqual(w.bundle.id,old)
        self.assertEqual(self.app.validation_controller.report_state(),'STALE')
        self.assertEqual(validate_plan(make_request(s))['status'],'PASS')
        w.plant_table.select(PID);w.property_table.select('plant.drained_soil_required:0');self.pump()
        self.assertIn('Значение: Нет',w.plant_info.get('1.0','end'))
        path=self.folder/'saved.gcp';save_project(s,path)
        loaded,_=load_project(path);self.app.adopt(loaded);self.pump()
        self.assertEqual(w.bundle.id,loaded.reference.id)
        self.assertIn('снимок',loaded.reference_notice)
        self.assert_clean()

    def test_footer_is_visible_at_small_window_and_scale(self):
        self.root.tk.call('tk','scaling',1.8)
        w=self.window();w.geometry('920x620');self.pump(.15)
        for b in (w.close_button,w.update_button,w.build_button,w.export_button):
            self.assertTrue(b.winfo_ismapped())
            self.assertLessEqual(b.winfo_rooty()+b.winfo_height(),w.winfo_rooty()+w.winfo_height())
        self.assert_clean()

if __name__=='__main__':unittest.main()
