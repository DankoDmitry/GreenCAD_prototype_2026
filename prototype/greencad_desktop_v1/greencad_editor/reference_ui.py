"""Read-only inspector of the exact bundle pinned to the current editor session.

Views never open the old JSON indices or fetch external standards. No botanical
values are created, no normative rules enabled. Updates require explicit consent.
"""
from __future__ import annotations
from copy import deepcopy
import json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

from .reference_data import (GROUPS, default_bundle, fresh_default_bundle, read_bundle,
                             write_bundle, digest)
from .errors import EditorError
from .checks import RULE_LABELS
from .checks_ui import SOIL_LABELS


def value_text(value, status='KNOWN'):
    if status!='KNOWN' or value is None:return 'Нет данных'
    if type(value) is bool:return 'Да' if value else 'Нет'
    if isinstance(value,(list,dict)):return json.dumps(value,ensure_ascii=False)
    if isinstance(value,float):return f'{value:.9g}'
    return str(value)


def location_text(row):
    if not row:return 'Источник записи не привязан к ячейке.'
    if row.get('sheet'):
        position=row['sheet']+'!'+row.get('range',str(row.get('row','')))
        if row.get('cells',{}).get('value'):position+=' (значение: '+row['cells']['value']+')'
    else:position=row.get('pointer',row.get('record_id',''))
    return row.get('file','')+' / '+position+'\nSHA-256: '+row.get('file_sha256','не задан')


def set_text(widget,text):
    widget.configure(state='normal');widget.delete('1.0','end');widget.insert('1.0',text);widget.configure(state='disabled')


class DataTable(ttk.Frame):
    def __init__(self,master,columns,selected,height=10):
        super().__init__(master)
        self.rowconfigure(1,weight=1);self.columnconfigure(0,weight=1)
        self.rows=[];self.selected=selected;self.query=tk.StringVar()
        entry=ttk.Entry(self,textvariable=self.query);entry.grid(row=0,column=0,sticky='ew',pady=(0,6))
        self.tree=ttk.Treeview(self,columns=[c[0] for c in columns],show='headings',selectmode='browse',height=height)
        for key,label,width in columns:
            self.tree.heading(key,text=label,command=lambda k=key:self.sort(k))
            self.tree.column(key,width=width,minwidth=65,stretch=key in ('name','value','parameter'))
        self.tree.grid(row=1,column=0,sticky='nsew')
        sy=ttk.Scrollbar(self,orient='vertical',command=self.tree.yview);sy.grid(row=1,column=1,sticky='ns')
        sx=ttk.Scrollbar(self,orient='horizontal',command=self.tree.xview);sx.grid(row=2,column=0,sticky='ew')
        self.tree.configure(yscrollcommand=sy.set,xscrollcommand=sx.set)
        self.count=ttk.Label(self,style='Muted.TLabel');self.count.grid(row=3,column=0,sticky='w',pady=(3,0))
        self.query.trace_add('write',lambda *_:self.populate())
        self.tree.bind('<<TreeviewSelect>>',lambda e:self._select())
        self.keys=[c[0] for c in columns]
        self._reverse={}

    def sort(self,key):
        i=self.keys.index(key);reverse=self._reverse.get(key,False)
        self.rows.sort(key=lambda r:str(r[1][i]).casefold(),reverse=reverse)
        self._reverse[key]=not reverse;self.populate()

    def set_rows(self,rows):
        self.rows=list(rows);self.populate()

    def populate(self):
        old=self.tree.selection();query=self.query.get().strip().casefold()
        self.tree.delete(*self.tree.get_children())
        for rid,values,search in self.rows:
            if not query or query in (search+' '+' '.join(str(x) for x in values)).casefold():
                self.tree.insert('','end',iid=rid,values=values)
        ids=self.tree.get_children()
        if old and self.tree.exists(old[0]):self.tree.selection_set(old[0])
        elif ids:self.tree.selection_set(ids[0])
        self.count.configure(text=f'Показано {len(ids)} из {len(self.rows)} • поиск по тексту / ID')
        self._select()

    def _select(self):
        ids=self.tree.selection()
        if self.selected:self.selected(ids[0] if ids else None)

    def select(self,rid):
        if not self.tree.exists(rid):self.query.set('')
        if self.tree.exists(rid):
            self.tree.selection_set(rid);self.tree.see(rid);self._select()


def detail_box(parent):
    box=ttk.Frame(parent);box.rowconfigure(0,weight=1);box.columnconfigure(0,weight=1)
    text=tk.Text(box,wrap='word',height=8,background='#EDF2EC',relief='flat',padx=12,pady=10,state='disabled')
    text.grid(row=0,column=0,sticky='nsew')
    scroll=ttk.Scrollbar(box,command=text.yview);scroll.grid(row=0,column=1,sticky='ns');text.configure(yscrollcommand=scroll.set)
    return box,text


class ReferenceWindow(tk.Toplevel):
    def __init__(self,controller):
        app=controller.app
        super().__init__(app.root)
        self.controller=controller;self.app=app;self.bundle=app.session.reference
        self.title('Справочники и условия проекта — используемые данные')
        self.geometry('1240x790');self.minsize(900,600)
        # Non-modal: the user may continue editing; changed() refreshes this view.
        self.transient(app.root)
        outer=ttk.Frame(self,padding=16);outer.pack(fill='both',expand=True)
        outer.rowconfigure(2,weight=1);outer.columnconfigure(0,weight=1)
        ttk.Label(outer,text='ДАННЫЕ, КОТОРЫЕ ИСПОЛЬЗУЕТ РАСЧЁТ',style='Header.TLabel').grid(row=0,column=0,sticky='w')
        self.summary=ttk.Label(outer,style='Muted.TLabel',wraplength=1120)
        self.summary.grid(row=1,column=0,sticky='w',pady=(5,10))
        self.tabs=ttk.Notebook(outer);self.tabs.grid(row=2,column=0,sticky='nsew')
        self.plants_tab=ttk.Frame(self.tabs,padding=10)
        self.rules_tab=ttk.Frame(self.tabs,padding=10)
        self.site_tab=ttk.Frame(self.tabs,padding=10)
        self.sources_tab=ttk.Frame(self.tabs,padding=10)
        for f,title in [(self.plants_tab,'Растения'),(self.rules_tab,'Правила размещения'),
                        (self.site_tab,'Условия участка'),(self.sources_tab,'Источники и версия')]:
            self.tabs.add(f,text=title);f.columnconfigure(0,weight=1);f.rowconfigure(0,weight=1)
        self.current_plant=None;self._property_rows={}
        self.plant_table=DataTable(self.plants_tab,[('name','Растение',320),('group','Группа',175),
             ('size','Ø кроны, м',135),('drain','Требуется дренаж',155),('known','Фактов',85)],self.select_plant,height=7)
        self.plant_table.grid(row=0,column=0,sticky='nsew')
        lower=ttk.Panedwindow(self.plants_tab,orient='horizontal');lower.grid(row=1,column=0,sticky='nsew',pady=(8,0))
        self.plants_tab.rowconfigure(1,weight=2)
        self.property_table=DataTable(lower,[('parameter','Характеристика',245),('value','Значение',110),
            ('usage','Использование',185)],self.select_property,height=8)
        lower.add(self.property_table,weight=3)
        info,self.plant_info=detail_box(lower);lower.add(info,weight=2)
        self._rule_rows={};self._site_rows={};self._source_rows={}
        self.rule_table,self.rule_info=self._split_tab(self.rules_tab,[('on','Включено',85),('name','Проверка',340),
               ('state','Реализация',185),('parameter','Эффективное условие',280)],self.select_rule)
        self.site_table,self.site_info=self._split_tab(self.site_tab,[('name','Параметр / поле',300),('value','Значение',220),
               ('scope','Область действия',190),('usage','Использование',220)],self.select_site)
        self.source_table,self.source_info=self._split_tab(self.sources_tab,[('name','Источник / файл',430),('state','Статус',440)],self.select_source)
        footer=ttk.Frame(outer);footer.grid(row=3,column=0,sticky='ew',pady=(12,0))
        self.close_button=ttk.Button(footer,text='Закрыть',style='Accent.TButton',command=self.destroy);self.close_button.pack(side='right')
        self.update_button=ttk.Button(footer,text='Обновить пакет…',command=controller.load_file);self.update_button.pack(side='left')
        self.build_button=ttk.Button(footer,text='Собрать из Excel…',command=controller.build_workbooks);self.build_button.pack(side='left',padx=6)
        self.export_button=ttk.Button(footer,text='Экспорт снимка…',command=controller.export);self.export_button.pack(side='left')
        ttk.Label(footer,text='Только просмотр. Изменение сценария — в «Выбрать проверки…».',style='Muted.TLabel',wraplength=430).pack(side='left',padx=12)
        self.bind('<Escape>',lambda e:self.destroy())
        self.refresh()

    def _split_tab(self,tab,columns,select):
        pane=ttk.Panedwindow(tab,orient='vertical');pane.grid(row=0,column=0,sticky='nsew')
        table=DataTable(pane,columns,select,height=11);pane.add(table,weight=3)
        box,info=detail_box(pane);pane.add(box,weight=2)
        return table,info

    def refresh(self):
        if not self.app.session:return
        self.bundle=self.app.session.reference
        b=self.bundle;self._last_key=self.controller.view_key()
        info=b.summary()
        self.summary.configure(text=f"Пакет {b.id[:12]}… • {info['plants']} растений • {info['values']} фактов • {info['executable_rules']} исполняемых проверок\n"
            'Таблицы и расчёт используют один снимок. Нет данных ≠ разрешено. Численные отступы — TEST, не нормативный профиль.')
        rows=[]
        for p in b.plants():
            size=b.value(p['id'],'project.crown_diameter')
            drain=b.value(p['id'],'plant.drained_soil_required')
            rows.append((p['id'],(p['name'],GROUPS.get(p['group'],p['group']),value_text(size['value'],size['status']),
                 value_text(drain['value'],drain['status']),len(b.values_for(p['id']))),p['id']))
        self.plant_table.set_rows(rows)
        self.build_rules();self.build_site();self.build_sources()

    def select_plant(self,pid):
        self.current_plant=pid
        if not hasattr(self,'property_table'):return
        self._property_rows={}
        if not pid:
            self.property_table.set_rows([]);set_text(self.plant_info,'Выберите растение.');return
        b=self.bundle;rows=[]
        # Every definition remains visible, even when no numeric fact is available.
        definitions=[p for p in b.parameters() if p.get('entity') in ('plant','project_state','nursery_stock')]
        definitions.sort(key=lambda p: (not any(v['parameter_id']==p['id'] and v['status']=='KNOWN' for v in b.values_for(pid)) and p['id'] not in ('plant.name','plant.class','plant.foliage_group'), p['label']))
        for parameter in definitions:
            key=parameter['id'];facts=[v for v in b.values_for(pid) if v['parameter_id']==key]
            if not facts:facts=[b.value(pid,key)]
            for index,fact in enumerate(facts):
                rid=key+':'+str(index);self._property_rows[rid]=(parameter,fact)
                consumers=b.consumers(key)
                enabled=set(self.app.session.check_settings['enabled'])
                used=[r for r in consumers if r in enabled]
                usage=('Включено: '+', '.join(used)) if used else ('Подключено; выключено' if consumers else 'Справочно / не подключено')
                label=parameter['label']
                context=fact.get('context',{})
                if context:label+=' • '+', '.join(map(str,context.values()))
                rows.append((rid,(label,value_text(fact['value'],fact['status']),usage),key+' '+fact.get('id','')))
        self.property_table.set_rows(rows)

    def select_property(self,rid):
        if not hasattr(self,'plant_info'):return
        if rid not in self._property_rows:
            set_text(self.plant_info,'Выберите характеристику.');return
        p,fact=self._property_rows[rid];b=self.bundle
        plant=b.plant(self.current_plant)
        lines=[plant['name'],self.current_plant,'',p['label']+' ['+p['id']+']',
              'Значение: '+value_text(fact['value'],fact['status']),
              'Статус: '+fact['status'],'Единица: '+p.get('unit','—'),
              'Смысл: '+p.get('meaning',''),'','Источник значения:',location_text(fact.get('location')),
              'Запись: '+fact.get('id','отсутствует'),'Основание: '+fact.get('source_id','')+'; '+fact.get('locator',''),
              'Исходная формулировка: '+str(fact.get('raw','не дана')),
              '', 'Потребители: '+(', '.join(b.consumers(p['id'])) or 'Нет исполняемой проверки'),
              '', 'Размеры материала при поставке не являются размерами взрослого растения. Радиус значка не подставляется в справочник.']
        set_text(self.plant_info,'\n'.join(lines))

    def build_rules(self):
        b=self.bundle;settings=self.app.session.check_settings;rows=[];self._rule_rows={}
        used=set()
        for r in b.rules():
            used.update(r.get('registry_ids',[]));rid=r['id'];self._rule_rows[rid]=('runtime',r)
            if 'threshold_m' in r:
                effective=str(settings['thresholds_m'].get(rid,r['threshold_m']))+' м • TEST'
            elif r.get('plant_parameter'):
                effective=r['plant_parameter']+' → '+r['site_parameter']
            elif r['operator']=='range':effective='A1 участка ↔ диапазон проектного типа'
            else:effective=r.get('field_id','')
            rows.append((rid,('Да' if rid in settings['enabled'] else 'Нет',r['label'],{'PROJECT':'Проект / подключено','PROJECT_TEST':'TEST / подключено','SOURCE_CONDITION':'Каталог / подключено','ADVISORY':'Рекомендация'}.get(r['kind'],r['kind']),effective),rid))
        for r in b.table('rules'):
            if r['id'] in used:continue
            rid='pending:'+r['id'];self._rule_rows[rid]=('pending',r)
            rows.append((rid,('—',r['label'],'Не подключено',r.get('expression','')),r['id']))
        self.rule_table.set_rows(rows)

    def select_rule(self,rid):
        if rid not in self._rule_rows:set_text(self.rule_info,'Выберите правило.');return
        kind,r=self._rule_rows[rid];b=self.bundle;settings=self.app.session.check_settings
        lines=[r['label']+' ['+r['id']+']', 'Исполняется выбранным набором' if kind=='runtime' and r['id'] in settings['enabled'] else 'Сейчас не исполняется', '']
        if kind=='runtime':
            lines += [r['description'],'Обработчик: '+r['operator'],'Применимость: '+r.get('evaluation_scope',''),
                      'Связь: '+json.dumps(r.get('binding',{}),ensure_ascii=False)]
            if 'threshold_m' in r:
                override=r['id'] in settings['thresholds_m']
                lines += ['Эффективный порог: '+value_text(settings['thresholds_m'].get(r['id'],r['threshold_m']))+' м',
                          'Источник числа: '+('настройка текущего проекта' if override else 'тестовый профиль поставки'),
                          'Это не нормативный отступ. Корнезащита его не уменьшает.']
            if r.get('field_id'):
                origin=r.get('binding',{}).get('field_registry','plants')
                f=next((x for x in b.table('fields',origin) if x['id']==r['field_id']),{})
                lines += ['','Определение поля: '+f.get('method',''),
                          'Геометрия: '+str(r.get('roles',[])),location_text(b.location('fields',r['field_id'],origin))]
            for regid in r.get('registry_ids',[]):
                lines += ['Связь с реестром '+regid+':',location_text(b.location('rules',regid))]
            lines += ['Основание: '+s.get('source_id','')+'; '+s.get('locator','') for s in r.get('provenance',[])]
        else:
            lines += [r.get('expression',''),'Применимость: '+r.get('applicability',''),
                      'Состояние источника: '+r.get('status',''),location_text(b.location('rules',r['id'])),
                      'Справочная запись не становится исполняемой от включения галочки.']
            match=next((x for x in b.table('setbacks') if 'R.'+x['id']==r['id']),None)
            if match:lines += ['','Запись таблицы отступов (НЕ активирована):',json.dumps(match,ensure_ascii=False,indent=2)]
        set_text(self.rule_info,'\n'.join(lines))

    def build_site(self):
        s=self.app.session;b=self.bundle;rows=[];self._site_rows={}
        names={'A1':'Влажность A1 — условная шкала 0–1','territory':'Категория территории',
               'ordinary_territory':'Обычная территория без особого режима',**SOIL_LABELS}
        for key,value in s.check_settings['site'].items():
            rid='parameter:'+key;usage=[r['label'] for r in b.rules() if r.get('site_parameter')==key or (key=='A1' and r['operator']=='range') or (key in ('territory','ordinary_territory') and r['operator']=='territory')]
            description='Задано пользователем для всего участка; не измерение из DXF.\nНезаполненное значение не подменяется нулём.\nПараметр: '+key
            self._site_rows[rid]=description
            rows.append((rid,(names.get(key,key),value_text(value),'Весь участок', '; '.join(usage) or 'Справочно'),key))
        for tid in s.types:
            value=s.check_settings['type_ranges'].get(tid);rid='range:'+tid
            self._site_rows[rid]='Диапазон задан пользователем для проектного типа '+tid+'. Не ботаническая характеристика сорта и не размер знака.'
            rows.append((rid,('Диапазон A1 • '+s.types[tid].name,value_text(value),'Проектный тип '+tid,'R.moisture_range'),tid))
        for r in b.rules():
            if not r.get('object_types'):continue
            rid='geometry:'+r['id'];counts={oid:sum(f.type_id==oid for f in s.drawing.features) for oid in r['object_types']}
            self._site_rows[rid]='Источник: классифицированная геометрия DXF.\nЭто состав данных, не готовые значения расстояний.\n'+json.dumps(counts,ensure_ascii=False,indent=2)+'\nРоли: '+str(r.get('roles'))+'\nПодтверждено отсутствие: '+str(s.check_settings['confirmed_absent'])
            rows.append((rid,(r['label'],str(sum(counts.values()))+' объектов-кандидатов','По координате',r['field_id']),r['id']))
        self.site_table.set_rows(rows)

    def select_site(self,rid):
        set_text(self.site_info,self._site_rows.get(rid,'Выберите параметр.'))

    def build_sources(self):
        b=self.bundle;rows=[];self._source_rows={}
        self._source_rows['bundle']=json.dumps(b.summary(),ensure_ascii=False,indent=2)+'\n\n'+self.app.session.reference_notice+'\nСнимок сохраняется внутри .gcp v4. Проверки после открытия выполняются заново. Нормативные тексты этой доработкой не перепроверялись.'
        rows.append(('bundle',('Текущий рабочий пакет',b.id),'bundle'))
        for i,item in enumerate(b.origins()):
            rid='file:'+str(i);self._source_rows[rid]=json.dumps(item,ensure_ascii=False,indent=2)
            rows.append((rid,(item['file'],item['role']),item['sha256']))
        for origin in ('plants','site'):
            for src in b.table('sources',origin):
                rid=origin+':'+src['id'];self._source_rows[rid]=json.dumps(src,ensure_ascii=False,indent=2)
                rows.append((rid,(src.get('title',src['id']),src.get('status','')),rid))
        self.source_table.set_rows(rows)

    def select_source(self,rid):
        set_text(self.source_info,self._source_rows.get(rid,'Выберите источник.'))


class ReferenceController:
    def __init__(self,app):
        self.app=app;self.window=None
        row=app.validation_controller.check_button.master
        label=row.winfo_children()[-1]
        self.button=ttk.Button(row,text='Справочники…',command=app.show_reference)
        self.button.pack(side='left',padx=5,before=label)
        app.toolbuttons.append(self.button)

    def view_key(self):
        s=self.app.session
        return None if s is None else (id(s),s.reference.id,digest(s.check_settings),
             tuple((t.id,t.name) for t in s.types.values()),digest(s.drawing.layer_mapping))

    def show(self,plant_id=None):
        if not self.app.session or self.app.busy:return
        if not self.window or not self.window.winfo_exists():self.window=ReferenceWindow(self)
        else:self.window.refresh();self.window.lift()
        if plant_id:
            self.window.tabs.select(self.window.plants_tab);self.window.plant_table.select(plant_id)
        return self.window

    def changed(self):
        if self.window and self.window.winfo_exists() and self.window._last_key!=self.view_key():self.window.refresh()

    def apply_bundle(self,bundle):
        app=self.app
        if not app.session or app.busy:return False
        if app.session.reference.id==bundle.id:
            messagebox.showinfo('Справочники','Этот пакет уже используется.',parent=self.window or app.root);return False
        if not messagebox.askyesno('Обновление справочников',
            f'Переключить текущий проект на пакет {bundle.id[:12]}…?\n'
            'Старые отчёт и карта станут неактуальными. Исходные книги не изменяются.\n'
            'Для сохранения снимка на диск потребуется Ctrl+S. Обновление можно отменить через Undo.',parent=self.window or app.root):return False
        app.canvas.cancel_drag()
        app.session.set_reference(bundle);app.sync_reference();app.on_change();app.on_select(app.session.selected_id,None)
        return True

    def load_file(self):
        app=self.app
        if not app.session or app.busy:return
        path=filedialog.askopenfilename(parent=self.window or app.root,title='Готовый пакет справочников',
             filetypes=[('Пакет GreenCAD','*.json.gz *.json')])
        if not path:return
        try:self.apply_bundle(read_bundle(path))
        except EditorError as exc:messagebox.showerror('Пакет не применён',str(exc),parent=self.window or app.root)

    def build_workbooks(self):
        app=self.app
        if not app.session or app.busy:return
        plant=filedialog.askopenfilename(parent=self.window or app.root,title='Реестр растений v1 — Excel',filetypes=[('Excel','*.xlsx')])
        if not plant:return
        site=filedialog.askopenfilename(parent=self.window or app.root,title='Словарь территории v1 — Excel',filetypes=[('Excel','*.xlsx')])
        if not site:return
        from .reference_build import build_from_sources
        session=app.session
        def done(bundle):
            if app.session is session:self.apply_bundle(bundle)
        app.run_task(lambda:build_from_sources(registry_xlsx=plant,site_xlsx=site),done,'Проверка и сборка справочников…')

    def export(self):
        if not self.app.session or self.app.busy:return
        path=filedialog.asksaveasfilename(parent=self.window or self.app.root,defaultextension='.json.gz',initialfile='reference_snapshot.json.gz',filetypes=[('Пакет GreenCAD','*.json.gz')])
        if not path:return
        try:write_bundle(self.app.session.reference,path)
        except (EditorError,OSError) as exc:messagebox.showerror('Экспорт',str(exc),parent=self.window or self.app.root)
