"""Real Tk regression tests for template editing and explainable catalogue matching.

Xvfb is required on headless Linux. Increased Tk scale is not a native Windows DPI test.
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import asdict
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
from greencad_editor.templates import new_template
from greencad_editor.templates_ui import TypeDialog, CandidateDialog, FilterDialog
from greencad_editor.project_io import save_project, load_project
from greencad_editor.checks import make_request, validate_plan

PID='PER.perspective.conifer_tree.004'
REQ='plant.drained_soil_required'

@unittest.skipUnless(os.environ.get('DISPLAY') or os.name=='nt', 'Требуется Tk/Xvfb')
class TemplateGuiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.folder=Path(self.tmp.name)
        self.root=tk.Tk();self.app=EditorApp(self.root);self.errors=[]
        self.root.report_callback_exception=lambda kind,value,tb:self.errors.append(value)
        self.app.adopt(EditorSession(load_dxf(make_demo(self.folder/'site.dxf'))))
        self.s=self.app.session;self.pump()
    def tearDown(self):
        self.app.executor.shutdown(wait=True);self.app.admissibility_controller.shutdown()
        for job in self.root.tk.call('after','info'):
            try:self.root.tk.call('after','cancel',job)
            except tk.TclError:pass
        self.root.destroy();self.tmp.cleanup()
        self.assertEqual(self.errors,[])
    def pump(self, duration=.04):
        until=time.monotonic()+duration
        while time.monotonic()<until:self.root.update();time.sleep(.002)
    def click(self, widget):
        widget.event_generate('<Enter>');x=widget.winfo_width()//2;y=widget.winfo_height()//2
        widget.event_generate('<ButtonPress-1>',x=x,y=y);widget.event_generate('<ButtonRelease-1>',x=x,y=y);self.pump()
    def dialog(self):
        self.app.canvas.active_type='T1';d=self.app.edit_type();self.pump();return d
    def enable(self,d):d.enabled.set(True)
    def candidate(self,d):
        d.pick_plant();self.pump();return d.candidate_window
    def select(self, d, pid):d.table.select(pid);self.pump()
    def assert_visible(self,b,d):
        self.assertTrue(b.winfo_ismapped());self.assertGreater(b.winfo_width(),1)
        self.assertLessEqual(b.winfo_rooty()+b.winfo_height(),d.winfo_rooty()+d.winfo_height())
        self.assertLessEqual(b.winfo_rootx()+b.winfo_width(),d.winfo_rootx()+d.winfo_width())
        self.assertIs(d.winfo_containing(b.winfo_rootx()+b.winfo_width()//2,b.winfo_rooty()+b.winfo_height()//2),b)

    def test_real_save_changes_profile_not_catalogue(self):
        digest=self.s.reference.id;d=self.dialog();self.enable(d);d.name.set('Дерево А')
        d.limit_vars['crown_radius_m'].set('1,5');d.requirement_vars[REQ].set('Требуется')
        d.threshold_vars['project.gas'].set('4');d.root_mode.set('REQUIRED')
        self.click(d.save_button)
        t=self.s.types['T1'];self.assertEqual(t.name,'Дерево А');self.assertEqual(t.template['limits']['crown_radius_m'],1.5)
        self.assertTrue(t.template['requirements'][REQ]);self.assertEqual(t.template['minimum_distances_m']['project.gas'],4.)
        self.assertEqual(self.s.reference.id,digest);self.assertIsNone(t.plant_id)
        again=self.dialog();self.assertEqual(again.limit_vars['crown_radius_m'].get(),'1.5')
        self.assertEqual(again.root_mode.get(),'REQUIRED')

    def test_cancel_does_not_change_type(self):
        before=self.s.state();d=self.dialog();self.enable(d);d.name.set('discard');self.click(d.cancel_button)
        self.assertEqual(before,self.s.state())

    def test_invalid_numbers_no_partial_apply(self):
        before=self.s.state();d=self.dialog();self.enable(d);d.name.set('do not save');d.limit_vars['root_radius_m'].set('nan')
        with patch('greencad_editor.templates_ui.messagebox.showerror') as error:self.click(d.save_button)
        self.assertTrue(d.winfo_exists());error.assert_called_once();self.assertEqual(before,self.s.state())

    def test_candidates_use_actual_snapshot_and_sources(self):
        d=self.dialog();self.enable(d);d.requirement_vars[REQ].set('Требуется');c=self.candidate(d)
        self.assertEqual(len(c.results),329);self.select(c,PID)
        text=c.info.get('1.0','end');self.assertIn('V02205',text);self.assertIn('E2210',text)
        self.assertEqual(c.results[PID]['status'],'PASS');self.assertEqual(c.info.cget('state'),'disabled')
        c.query.set('Тсуга');self.pump();self.assertIn(PID,c.tree.get_children());self.assertLess(len(c.tree.get_children()),329)
        self.click(c.accept_button);self.assertEqual(d.plant_id,PID)
        self.assertIsNone(self.s.types['T1'].plant_id);self.assertIs(self.root.grab_current(),d)
        self.click(d.save_button);self.assertEqual(self.s.types['T1'].plant_id,PID)

    def test_unknown_candidate_requires_explicit_draft_confirmation(self):
        d=self.dialog();self.enable(d);d.limit_vars['crown_radius_m'].set('1.5');c=self.candidate(d);self.select(c,PID)
        self.assertEqual(c.results[PID]['status'],'UNKNOWN')
        with patch('greencad_editor.templates_ui.messagebox.askyesno',return_value=False):self.click(c.accept_button)
        self.assertTrue(c.winfo_exists());self.assertIsNone(d.plant_id)
        with patch('greencad_editor.templates_ui.messagebox.askyesno',return_value=True):self.click(c.accept_button)
        self.assertEqual(d.plant_id,PID)
        with patch('greencad_editor.templates_ui.messagebox.askyesno',return_value=True):self.click(d.save_button)
        self.s.add('T1',20,10);r=validate_plan(make_request(self.s));self.assertEqual(r['status'],'UNKNOWN')
        self.assertTrue(any(c['rule_id']=='template.catalog_match' and c['status']=='UNKNOWN' for c in r['placements'][0]['checks']))

    def test_failed_candidate_cannot_be_assigned(self):
        d=self.dialog();self.enable(d);d.requirement_vars[REQ].set('Не требуется');c=self.candidate(d);self.select(c,PID)
        self.assertEqual(c.results[PID]['status'],'FAIL')
        with patch('greencad_editor.templates_ui.messagebox.showwarning') as warning:self.click(c.accept_button)
        warning.assert_called_once();self.assertTrue(c.winfo_exists());self.assertIsNone(d.plant_id)

    def test_result_status_filter_and_missing_flower_group(self):
        d=self.dialog();self.enable(d);d.category.set('herbaceous');c=self.candidate(d)
        c.status_filter.current(1);c.status_filter.event_generate('<<ComboboxSelected>>');self.pump()
        self.assertEqual(len(c.tree.get_children()),0)
        c.status_filter.current(3);c.status_filter.event_generate('<<ComboboxSelected>>');self.pump()
        self.assertEqual(len(c.tree.get_children()),329)

    def test_filter_add_edit_and_remove(self):
        d=self.dialog();self.enable(d);d.tabs.select(d.filters_tab);d.add_filter();self.pump()
        f=next(w for w in d.winfo_children() if isinstance(w,FilterDialog))
        f.parameter.current(f.ids.index(REQ));f.parameter_changed();f.value.set('Да');self.click(f.accept_button)
        self.assertEqual(d.filters[0]['value'],True);self.assertIs(self.root.grab_current(),d)
        d.filter_table.select('0');d.edit_filter();self.pump()
        f=next(w for w in d.winfo_children() if isinstance(w,FilterDialog))
        f.value.set('Нет');self.click(f.accept_button);self.assertEqual(d.filters[0]['value'],False)
        d.filter_table.select('0');d.remove_filter();self.assertEqual(d.filters,[])

    def test_invalid_filter_no_parent_mutation(self):
        d=self.dialog();self.enable(d);d.add_filter();self.pump()
        f=next(w for w in d.winfo_children() if isinstance(w,FilterDialog))
        f.value.set('')
        with patch('greencad_editor.templates_ui.messagebox.showerror') as err:self.click(f.accept_button)
        err.assert_called_once();self.assertEqual(d.filters,[]);self.assertTrue(f.winfo_exists())

    def test_dependencies_explain_missing_active_requirement(self):
        settings=deepcopy(self.s.check_settings);settings['enabled']=['R.domain','R.soil.drained'];self.s.set_check_settings(settings)
        d=self.dialog();self.enable(d);d.tabs.select(d.ready_tab);self.pump()
        self.assertEqual(d.ready_rows['R.soil.drained']['status'],'UNKNOWN')
        d.requirement_vars[REQ].set('Требуется');d.refresh_ready()
        self.assertEqual(d.ready_rows['R.soil.drained']['status'],'READY')

    def test_binding_schedules_full_recheck_without_moving(self):
        p=self.s.add('T1',20,10);before=(p.x,p.y);d=self.dialog();self.enable(d);d.plant_id=PID
        with patch.object(self.app.validation_controller,'run') as run:self.click(d.save_button);self.pump();run.assert_called_once()
        self.assertEqual((self.s.placements[p.id].x,self.s.placements[p.id].y),before)
        self.assertEqual(self.s.types['T1'].plant_id,PID)

    def test_mass_binding_keeps_overrides_unless_requested(self):
        p=self.s.add('T1',20,10);self.s.change(p.id,plant_id='REC.main.conifer_tree.001')
        d=self.dialog();self.enable(d);d.plant_id=PID
        with patch.object(self.app.validation_controller,'run'):self.click(d.save_button)
        self.assertEqual(self.s.placements[p.id].plant_id,'REC.main.conifer_tree.001')
        d=self.dialog();d.all_overrides.set(True)
        with patch.object(self.app.validation_controller,'run'):self.click(d.save_button)
        self.assertIsNone(self.s.placements[p.id].plant_id)

    def test_copy_delete_and_undo_refresh_type_list(self):
        d=self.dialog();self.enable(d);d.limit_vars['crown_radius_m'].set('1.5');self.click(d.save_button)
        before=set(self.s.types);self.app.copy_type();self.pump();created=(set(self.s.types)-before).pop()
        self.assertEqual(self.s.types[created].template,self.s.types['T1'].template)
        with patch('greencad_editor.ui.messagebox.askyesno',return_value=True):self.app.delete_type()
        self.assertNotIn(created,self.s.types);self.app.undo();self.pump();self.assertIn(created,self.s.types)

    def test_cannot_delete_type_with_placements(self):
        self.s.add('T1',20,10);self.app.canvas.active_type='T1'
        with patch('greencad_editor.ui.messagebox.askyesno',return_value=True),patch('greencad_editor.ui.messagebox.showerror') as err:self.app.delete_type()
        err.assert_called_once();self.assertIn('T1',self.s.types)

    def test_close_can_apply_discard_or_cancel(self):
        d=self.dialog();self.enable(d);d.name.set('changed')
        with patch('greencad_editor.templates_ui.messagebox.askyesnocancel',return_value=None):d.request_close()
        self.assertTrue(d.winfo_exists());self.assertNotEqual(self.s.types['T1'].name,'changed')
        with patch('greencad_editor.templates_ui.messagebox.askyesnocancel',return_value=True):d.request_close()
        self.assertEqual(self.s.types['T1'].name,'changed')
        d=self.dialog();d.name.set('discard')
        with patch('greencad_editor.templates_ui.messagebox.askyesnocancel',return_value=False):d.request_close()
        self.assertEqual(self.s.types['T1'].name,'changed')

    def test_profile_roundtrip_reopen_gui(self):
        d=self.dialog();self.enable(d);d.root_mode.set('FORBIDDEN');d.requirement_vars[REQ].set('Не требуется');self.click(d.save_button)
        path=self.folder/'project.gcp';save_project(self.s,path);s,_=load_project(path);self.app.adopt(s);self.s=s;self.pump()
        d=self.dialog();self.assertEqual(d.root_mode.get(),'FORBIDDEN');self.assertEqual(d.requirement_vars[REQ].get(),'Не требуется')

    def test_map_dialog_can_calculate_template_without_assigned_plant(self):
        from greencad_editor.admissibility_ui import MapOptionsDialog
        t=deepcopy(self.s.types['T1']);t.template=new_template()
        t.template['limits']['crown_radius_m']=1.5;t.plant_id=PID
        self.s.set_type(t);self.app.on_change()
        ctl=self.app.admissibility_controller;ctl.choose();self.pump();d=ctl.options_window
        self.assertIsInstance(d,MapOptionsDialog)
        d.template_only.set(True);d.override.set(True);d.update_description();d.step.set('5')
        self.assertFalse(d.override.get());self.click(d.build_button)
        until=time.monotonic()+15
        while self.app.busy and time.monotonic()<until:self.pump()
        self.assertFalse(self.app.busy);self.assertEqual(ctl.state(),'CURRENT')
        report=ctl.result.kernel.inspect(20,10)
        self.assertEqual(report['status'],'PASS');self.assertIsNone(report['plant_id'])
        self.assertFalse(self.s.placements)

    def test_fixed_footer_at_small_window_and_scale(self):
        self.root.tk.call('tk','scaling',1.8);d=self.dialog();self.enable(d);d.geometry('800x550');self.pump()
        for tab in d.tabs.tabs():
            d.tabs.select(tab);self.pump()
            for b in (d.save_button,d.cancel_button,d.pick_button):self.assert_visible(b,d)
        c=self.candidate(d);c.geometry('820x530');self.pump()
        for b in (c.accept_button,c.clear_button):self.assert_visible(b,c)

if __name__=='__main__':unittest.main()
