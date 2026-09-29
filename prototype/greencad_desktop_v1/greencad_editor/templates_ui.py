"""Editors for project assumptions and a read-only, explainable candidate list.

The catalogue snapshot is never modified here. A candidate is NOT automatically
approved for every site position. All dialogs have fixed action bars.
"""
from __future__ import annotations
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import json
import tkinter as tk
from tkinter import ttk, messagebox, colorchooser

from .errors import EditorError
from .models import ProjectType, finite
from .templates import (new_template, validate_type_reference, match_catalog, match_plant,
                        CATEGORY_LABELS, ROOT_MODES, LIMITS, SOIL_REQUIREMENTS,
                        available_parameters, operators_for, OP_LABELS, required_inputs)
from .reference_data import default_bundle
from .reference_ui import DataTable, detail_box, set_text, value_text, location_text
from .checks_ui import _ScrollableTab

CANDIDATE_STATUS = {'PASS':'Соответствует критериям','UNKNOWN':'Недостаточно данных','FAIL':'Не соответствует'}
BOOL_CHOICES = {'Не задано':None, 'Требуется':True, 'Не требуется':False}


def optional_number(text):
    return None if not text.strip() else finite(text.strip().replace(',','.'), 'Число')


class _ChildDialog(tk.Toplevel):
    def destroy(self):
        parent = self.master
        super().destroy()
        if isinstance(parent, tk.Toplevel):
            try:
                if parent.winfo_exists(): parent.grab_set()
            except tk.TclError:
                pass


class CandidateDialog(_ChildDialog):
    def __init__(self, master, spec, reference, callback, current=None):
        super().__init__(master)
        self.title('Растения для проектного типа — подбор по известным данным')
        self.geometry('1120x740');self.minsize(820,530)
        self.transient(master);self.grab_set()
        self.spec=deepcopy(spec);self.reference=reference;self.callback=callback
        self.results={r['plant_id']:r for r in match_catalog(spec,reference)}
        outer=ttk.Frame(self,padding=16);outer.pack(fill='both',expand=True)
        outer.columnconfigure(0,weight=1);outer.rowconfigure(3,weight=3);outer.rowconfigure(4,weight=2)
        counts=Counter(r['status'] for r in self.results.values())
        ttk.Label(outer,text=f'{spec.id} — {spec.name}',style='Header.TLabel').grid(row=0,column=0,sticky='w')
        self.summary=ttk.Label(outer,style='Muted.TLabel',wraplength=1020,
            text=f"Соответствует: {counts['PASS']} • нет данных: {counts['UNKNOWN']} • не соответствует: {counts['FAIL']}. "
            'Это фильтр каталога, не пространственная проверка. Пустой критерий не ограничивает; неизвестное свойство не доказывает соответствие.')
        self.summary.grid(row=1,column=0,sticky='ew',pady=(6,8))
        self.status_filter=ttk.Combobox(outer,values=['Все','Соответствуют','Недостаточно данных','Не соответствуют'],state='readonly',width=28)
        self.status_filter.grid(row=2,column=0,sticky='w',pady=(0,8));self.status_filter.current(0)
        box,self.info=detail_box(outer);box.grid(row=4,column=0,sticky='nsew',pady=(10,0))
        self.table=DataTable(outer,[('name','Растение',365),('status','Результат',215),('group','Группа',140),('id','ID',320)],self.selected,height=9)
        self.table.grid(row=3,column=0,sticky='nsew');self.tree=self.table.tree;self.query=self.table.query
        self.tree.tag_configure('PASS',foreground='#205B38');self.tree.tag_configure('UNKNOWN',foreground='#886315');self.tree.tag_configure('FAIL',foreground='#A63636')
        self.status_filter.bind('<<ComboboxSelected>>',lambda e:self.populate())
        self.tree.bind('<Double-1>',lambda e:self.accept());self.tree.bind('<Return>',lambda e:self.accept())
        footer=ttk.Frame(outer);footer.grid(row=5,column=0,sticky='ew',pady=(12,0))
        self.clear_button=ttk.Button(footer,text='Не назначать растение',command=lambda:self.finish(None));self.clear_button.pack(side='left')
        self.accept_button=ttk.Button(footer,text='Назначить выбранное',style='Accent.TButton',command=self.accept);self.accept_button.pack(side='right')
        self.cancel_button=ttk.Button(footer,text='Отмена',command=self.destroy);self.cancel_button.pack(side='right',padx=8)
        self.bind('<Escape>',lambda e:self.destroy())
        self.populate()
        if current:self.table.select(current)

    def populate(self):
        chosen={0:None,1:'PASS',2:'UNKNOWN',3:'FAIL'}[self.status_filter.current()]
        self.table.set_rows([(r['plant_id'],(r['name'],CANDIDATE_STATUS[r['status']],r['group'],r['plant_id']),r['name'])
                            for r in self.results.values() if chosen is None or r['status']==chosen])
        for rid in self.tree.get_children():self.tree.item(rid,tags=(self.results[rid]['status'],))

    def selected(self,pid):
        if not pid or pid not in self.results:set_text(self.info,'Выберите запись для объяснения.');return
        row=self.results[pid]
        lines=[row['name']+' ['+pid+']',CANDIDATE_STATUS[row['status']],row['scope'],'']
        for check in row['checks']:
            lines.extend([CANDIDATE_STATUS[check['status']]+' — '+check['label'],
                          'Операция: '+OP_LABELS.get(check['operator'],'Требование растения не строже шаблона'),
                          'В каталоге: '+value_text(check['actual'])+'; условие: '+value_text(check['required']),
                          check['reason']])
            record=check['value_record']
            if record.get('id'):lines.append('ID записи: '+record['id'])
            if record.get('raw'):lines.append('Исходная запись: '+record['raw'])
            lines.append('Источник: '+record.get('source_id','не указан')+'; '+record.get('locator',''))
            if record.get('location'):lines.append(location_text(record['location']))
            lines.append('')
        lines.extend(row['unverified'])
        lines.append('После назначения выполняется повторная проверка существующих посадок, не авторасстановка.')
        set_text(self.info,'\n'.join(lines))

    def finish(self,pid):
        self.callback(pid);self.destroy()

    def accept(self):
        selected=self.tree.selection()
        if not selected:return
        row=self.results[selected[0]]
        if row['status']=='FAIL':
            messagebox.showwarning('Растение не подходит','В описании ниже указаны несоответствующие критерии. Измените шаблон или выберите другую запись.',parent=self);return
        if row['status']=='UNKNOWN' and not messagebox.askyesno('Только черновое назначение',
            'Соответствие не подтверждено: часть характеристик неизвестна. Назначить как черновик? В проверках сохранится «Недостаточно данных».',parent=self,default='no'):return
        self.finish(row['plant_id'])


class FilterDialog(_ChildDialog):
    def __init__(self,master,reference,callback,current=None):
        super().__init__(master);self.title('Критерий подбора растения')
        self.geometry('820x490');self.minsize(620,430);self.transient(master);self.grab_set()
        self.reference=reference;self.callback=callback;self.parameters=available_parameters(reference)
        self.ids=[p['id'] for p in self.parameters]
        outer=ttk.Frame(self,padding=18);outer.pack(fill='both',expand=True);outer.columnconfigure(0,weight=1);outer.rowconfigure(1,weight=1)
        ttk.Label(outer,text='КРИТЕРИЙ ИЗ РЕЕСТРА ХАРАКТЕРИСТИК',style='Header.TLabel').grid(row=0,column=0,sticky='w')
        scroll=_ScrollableTab(outer);scroll.grid(row=1,column=0,sticky='nsew');f=scroll.content
        self.parameter=ttk.Combobox(f,values=[p['label']+' ['+p['id']+']' for p in self.parameters],state='readonly',width=75);self.parameter.pack(fill='x',pady=8)
        self.operation=ttk.Combobox(f,state='readonly');self.operation.pack(fill='x',pady=5)
        self.value=tk.StringVar();self.entry=ttk.Entry(f,textvariable=self.value);self.entry.pack(fill='x',pady=8)
        self.explanation=ttk.Label(f,wraplength=700,style='Muted.TLabel');self.explanation.pack(anchor='w',pady=8)
        self.context_ids=[None]+[x['id'] for x in reference.territories()]
        self.context=ttk.Combobox(f,state='readonly',values=['Без контекста']+[x['label'] for x in reference.territories()]);self.context.current(0);self.context.pack(fill='x',pady=5)
        footer=ttk.Frame(outer);footer.grid(row=2,column=0,sticky='ew',pady=(10,0))
        self.accept_button=ttk.Button(footer,text='Добавить критерий',style='Accent.TButton',command=self.accept);self.accept_button.pack(side='right')
        ttk.Button(footer,text='Отмена',command=self.destroy).pack(side='right',padx=7)
        self.parameter.bind('<<ComboboxSelected>>',self.parameter_changed)
        self.parameter.current(self.ids.index(current['parameter_id']) if current else 0);self.parameter_changed()
        if current:
            self.operation.current(self.ops.index(current['operator']))
            v=current['value'];self.value.set(('Да' if v else 'Нет') if type(v) is bool else ('; '.join(map(str,v)) if isinstance(v,list) else str(v)))
            key=current.get('context',{}).get('territory_id')
            if key in self.context_ids:self.context.current(self.context_ids.index(key))
        self.bind('<Escape>',lambda e:self.destroy())
        for event in ('<MouseWheel>','<Button-4>','<Button-5>'):self.bind(event,scroll.scroll)

    def parameter_changed(self,event=None):
        p=self.parameters[self.parameter.current()];self.ops=operators_for(p['dtype'])
        self.operation.configure(values=[OP_LABELS[k] for k in self.ops]);self.operation.current(0)
        self.explanation.configure(text=f"{p['meaning']}\nТип: {p['dtype']}; единица: {p['unit']}. "
            'Для логического значения: Да / Нет. Для диапазона или списка разделитель — точка с запятой. '
            'Например 1; 3. Пустое поле не сохраняется как критерий. Неизвестное значение растения не считается подходящим.')
        self.context.configure(state='readonly' if p['id']=='plant.territory_mark' else 'disabled')
        if p['id']!='plant.territory_mark':self.context.current(0)

    def accept(self):
        try:
            p=self.parameters[self.parameter.current()];op=self.ops[self.operation.current()]
            raw=self.value.get().strip()
            if not raw:raise EditorError('Задайте значение или отмените добавление критерия.')
            if op in ('between','covers'):value=[finite(v.strip().replace(',','.'),'Граница') for v in raw.split(';')]
            elif op in ('one_of','contains'):value=[v.strip() for v in raw.split(';')]
            elif p['dtype']=='boolean':
                values={'да':True,'нет':False,'true':True,'false':False}
                if raw.casefold() not in values:raise EditorError('Для булева параметра используйте Да или Нет.')
                value=values[raw.casefold()]
            elif p['dtype'] in ('number','integer'):value=finite(raw.replace(',','.'),'Число')
            else:value=raw
            context={};ctx=self.context_ids[self.context.current()]
            if ctx and p['id']=='plant.territory_mark':context={'territory_id':ctx}
            row={'parameter_id':p['id'],'operator':op,'value':value,'context':context}
            from .templates import validate_template
            profile=new_template();profile['filters']=[row];profile=validate_template(profile,self.reference)
            self.callback(profile['filters'][0]);self.destroy()
        except EditorError as exc:messagebox.showerror('Критерий',str(exc),parent=self)


class TypeDialog(tk.Toplevel):
    """One reusable type class, editable records. No per-species Python classes."""
    def __init__(self,master,project_type,plants,callback,session=None):
        super().__init__(master)
        self.title('Проектный тип — расчётный шаблон и растения')
        self.geometry('1040x790');self.minsize(800,550);self.transient(master);self.grab_set()
        self.spec=deepcopy(project_type);self.plants=plants;self.callback=callback;self.session=session
        self.reference=session.reference if session else default_bundle()
        self.plant_id=project_type.plant_id;self.base_profile=deepcopy(project_type.template or new_template())
        self.filters=deepcopy(self.base_profile['filters'])
        self.enabled=tk.BooleanVar(value=project_type.template is not None)
        self.name=tk.StringVar(value=project_type.name);self.radius=tk.StringVar(value=str(project_type.radius_m))
        self.color=tk.StringVar(value=project_type.color);self.category=tk.StringVar(value=project_type.category)
        self.symbol=tk.StringVar(value=project_type.symbol);self.all_overrides=tk.BooleanVar(value=False)
        outer=ttk.Frame(self,padding=16);outer.pack(fill='both',expand=True);outer.columnconfigure(0,weight=1);outer.rowconfigure(2,weight=1)
        ttk.Label(outer,text=f'{project_type.id} • ПРОЕКТНЫЙ ТИП',style='Header.TLabel').grid(row=0,column=0,sticky='w')
        ttk.Label(outer,text='Шаблон задаёт условия проекта, не изменяя ботанический справочник. Растение можно назначить позже. '
                  'Начальные отступы остаются тестовыми.',wraplength=960,style='Muted.TLabel').grid(row=1,column=0,sticky='ew',pady=(5,10))
        self.tabs=ttk.Notebook(outer);self.tabs.grid(row=2,column=0,sticky='nsew')
        basic=_ScrollableTab(self.tabs);profile=_ScrollableTab(self.tabs)
        self.filters_tab=ttk.Frame(self.tabs,padding=12);self.ready_tab=ttk.Frame(self.tabs,padding=12)
        self.tabs.add(basic,text='Название и знак');self.tabs.add(profile,text='Расчётный профиль')
        self.tabs.add(self.filters_tab,text='Критерии подбора');self.tabs.add(self.ready_tab,text='Что нужно проверкам')
        self.basic_tab=basic;self.profile_tab=profile
        f=basic.content;f.columnconfigure(1,weight=1)
        for i,(label,widget) in enumerate([
            ('Название',ttk.Entry(f,textvariable=self.name,width=55)),
            ('Категория',ttk.Combobox(f,textvariable=self.category,values=list(CATEGORY_LABELS),state='readonly')),
            ('Радиус условного знака, м',ttk.Entry(f,textvariable=self.radius)),
            ('Форма знака',ttk.Combobox(f,textvariable=self.symbol,values=['circle','point'],state='readonly')),
            ('Цвет #RRGGBB',ttk.Entry(f,textvariable=self.color))]):
            ttk.Label(f,text=label).grid(row=i,column=0,sticky='w',pady=7,padx=(0,14));widget.grid(row=i,column=1,sticky='ew',pady=7)
        ttk.Button(f,text='Выбрать цвет…',command=self.pick_color).grid(row=5,column=1,sticky='e')
        ttk.Label(f,text='tree — деревья; shrub — кустарники; herbaceous — цветы / травянистые; vine — лианы.\n'
                  'В текущем каталоге нет отдельной группы цветов: подбор не создаёт недостающие виды.\n'
                  'Радиус знака не участвует в расчётах. Резерв кроны задаётся на следующей вкладке.',
                  wraplength=890,style='Muted.TLabel').grid(row=6,column=0,columnspan=2,sticky='w',pady=14)
        self.plant_label=ttk.Label(f,text=self.plant_name(),wraplength=870);self.plant_label.grid(row=7,column=0,columnspan=2,sticky='w',pady=10)
        ttk.Button(f,text='Подобрать растение…',command=self.pick_plant).grid(row=8,column=0,sticky='w')
        ttk.Button(f,text='Оставить тип без растения',command=self.clear_plant).grid(row=8,column=1,sticky='w')
        ttk.Checkbutton(f,text='Назначить всему типу, сбросив индивидуальные растения у его посадок',variable=self.all_overrides).grid(row=9,column=0,columnspan=2,sticky='w',pady=14)
        f=profile.content;f.columnconfigure(1,weight=1)
        ttk.Checkbutton(f,text='Использовать самостоятельный расчётный шаблон',variable=self.enabled).grid(row=0,column=0,columnspan=2,sticky='w',pady=4)
        ttk.Label(f,text='Пустые габариты и критерии не ограничивают подбор. Но незаданное требование активной проверки даёт UNKNOWN.',
                  style='Muted.TLabel',wraplength=900).grid(row=1,column=0,columnspan=2,sticky='w',pady=8)
        self.limit_vars={};row=2
        for key,(_,_,label) in LIMITS.items():
            v=self.base_profile['limits'].get(key);var=tk.StringVar(value='' if v is None else str(v));self.limit_vars[key]=var
            ttk.Label(f,text=label).grid(row=row,column=0,sticky='w',pady=5);ttk.Entry(f,textvariable=var,width=18).grid(row=row,column=1,sticky='w',padx=12,pady=5);row+=1
        ttk.Label(f,text='Резерв кроны проверяется внутри зоны, вне исключений и между посадками, когда включены соответствующие правила.\n'
                  'Радиус корней и высота в этой версии — только условия подбора. Значения относятся к одному выбранному проектному состоянию.',
                  wraplength=880,style='Muted.TLabel').grid(row=row,column=0,columnspan=2,sticky='w',pady=8);row+=1
        self.state_label=tk.StringVar(value=self.base_profile['state_label'])
        ttk.Label(f,text='Описание расчётного состояния').grid(row=row,column=0,sticky='w');ttk.Entry(f,textvariable=self.state_label).grid(row=row,column=1,sticky='ew',padx=12);row+=1
        ttk.Separator(f).grid(row=row,column=0,columnspan=2,sticky='ew',pady=12);row+=1
        self.requirement_vars={}
        for key,label in SOIL_REQUIREMENTS.items():
            v=self.base_profile['requirements'].get(key);var=tk.StringVar(value=next(k for k,x in BOOL_CHOICES.items() if x is v));self.requirement_vars[key]=var
            ttk.Label(f,text=label).grid(row=row,column=0,sticky='w',pady=5)
            ttk.Combobox(f,textvariable=var,values=list(BOOL_CHOICES),state='readonly',width=21).grid(row=row,column=1,sticky='w',padx=12,pady=5);row+=1
        self.root_mode=tk.StringVar(value=self.base_profile['root_protection'])
        ttk.Label(f,text='Корнезащита: ANY / FORBIDDEN / REQUIRED').grid(row=row,column=0,sticky='w',pady=6)
        ttk.Combobox(f,textvariable=self.root_mode,values=list(ROOT_MODES),state='readonly',width=21).grid(row=row,column=1,sticky='w',padx=12);row+=1
        ttk.Label(f,text='ANY — возможна; FORBIDDEN — не использовать; REQUIRED — обязательна.\n'
                  'Это режим проекта, а не подтверждение технологии для вида. Отступы автоматически не уменьшаются.',
                  style='Muted.TLabel',wraplength=890).grid(row=row,column=0,columnspan=2,sticky='w',pady=8);row+=1
        bounds=self.base_profile['moisture_range'];self.moisture_lo=tk.StringVar(value=str(bounds[0]) if bounds else '');self.moisture_hi=tk.StringVar(value=str(bounds[1]) if bounds else '')
        ttk.Label(f,text='Диапазон A1 типа, 0–1 (обе границы)').grid(row=row,column=0,sticky='w')
        pair=ttk.Frame(f);pair.grid(row=row,column=1,sticky='w',padx=12)
        ttk.Entry(pair,textvariable=self.moisture_lo,width=10).pack(side='left');ttk.Label(pair,text=' — ').pack(side='left');ttk.Entry(pair,textvariable=self.moisture_hi,width=10).pack(side='left');row+=1
        ttk.Separator(f).grid(row=row,column=0,columnspan=2,sticky='ew',pady=12);row+=1
        ttk.Label(f,text='Минимумы этого типа: только повышают общие TEST-пороги',style='Header.TLabel').grid(row=row,column=0,columnspan=2,sticky='w',pady=5);row+=1
        self.threshold_vars={}
        for r in self.reference.rules():
            if r['operator'] not in ('distance','spacing'):continue
            v=self.base_profile['minimum_distances_m'].get(r['id']);var=tk.StringVar(value='' if v is None else str(v));self.threshold_vars[r['id']]=var
            ttk.Label(f,text=r['label']+', м').grid(row=row,column=0,sticky='w',pady=5);ttk.Entry(f,textvariable=var,width=18).grid(row=row,column=1,sticky='w',padx=12);row+=1
        # Filters: DataTable has its own scrolling and search, no nested expanding footer.
        self.filters_tab.rowconfigure(1,weight=1);self.filters_tab.columnconfigure(0,weight=1)
        ttk.Label(self.filters_tab,text='Дополнительные условия из зарегистрированных характеристик. Пустой список — без дополнительных ограничений.',wraplength=880,style='Muted.TLabel').grid(row=0,column=0,sticky='w',pady=(0,8))
        self.filter_table=DataTable(self.filters_tab,[('parameter','Характеристика',430),('op','Условие',150),('value','Значение',220)],None)
        self.filter_table.grid(row=1,column=0,sticky='nsew')
        bar=ttk.Frame(self.filters_tab);bar.grid(row=2,column=0,sticky='ew',pady=8)
        ttk.Button(bar,text='+ Критерий',command=self.add_filter).pack(side='left')
        ttk.Button(bar,text='Изменить',command=self.edit_filter).pack(side='left',padx=8)
        ttk.Button(bar,text='Удалить критерий',command=self.remove_filter).pack(side='left')
        self.refresh_filters()
        self.ready_tab.rowconfigure(1,weight=1);self.ready_tab.columnconfigure(0,weight=1)
        ttk.Label(self.ready_tab,text='Расчётный профиль не включает правила сам. Здесь показаны входы выбранных активных проверок.',wraplength=900,style='Muted.TLabel').grid(row=0,column=0,sticky='w',pady=8)
        self.ready_table=DataTable(self.ready_tab,[('name','Проверка',340),('value','Значение профиля',200),('status','Состояние',150)],self.ready_selected)
        self.ready_table.grid(row=1,column=0,sticky='nsew')
        self.ready_info=ttk.Label(self.ready_tab,wraplength=900,style='Muted.TLabel');self.ready_info.grid(row=2,column=0,sticky='ew',pady=8)
        ttk.Button(self.ready_tab,text='Обновить зависимости',command=self.refresh_ready).grid(row=3,column=0,sticky='w')
        self.ready_rows={}
        self.tabs.bind('<<NotebookTabChanged>>',self.tab_changed)
        footer=ttk.Frame(outer);footer.grid(row=3,column=0,sticky='ew',pady=(12,0))
        self.pick_button=ttk.Button(footer,text='Подобрать растения…',command=self.pick_plant);self.pick_button.pack(side='left')
        self.save_button=ttk.Button(footer,text='Сохранить тип',style='Accent.TButton',command=self.accept);self.save_button.pack(side='right')
        self.cancel_button=ttk.Button(footer,text='Отмена',command=self.destroy);self.cancel_button.pack(side='right',padx=8)
        ttk.Label(outer,text='Сохранить тип — применить к текущему проекту. Ctrl+S в редакторе — записать проект .gcp на диск.',
                  style='Muted.TLabel').grid(row=4,column=0,sticky='w',pady=(6,0))
        self.bind('<Control-Return>',lambda e:self.accept());self.bind('<Escape>',lambda e:self.request_close())
        self.protocol('WM_DELETE_WINDOW',self.request_close)
        for event in ('<MouseWheel>','<Button-4>','<Button-5>'):
            self.bind(event,lambda e:self.scroll_current(e))
        self.initial=asdict(self.gather())

    def scroll_current(self,event):
        selected=self.tabs.select()
        for tab in (self.basic_tab,self.profile_tab):
            if str(tab)==selected:return tab.scroll(event)

    def plant_name(self):
        return 'Растение типа: '+next((p['name'] for p in self.plants if p['id']==self.plant_id),'не назначено — работаем с шаблоном')

    def clear_plant(self):
        self.plant_id=None;self.plant_label.configure(text=self.plant_name())

    def pick_color(self):
        _,colour=colorchooser.askcolor(self.color.get(),parent=self)
        if colour:self.color.set(colour)

    def gather(self):
        data=asdict(self.spec);profile=deepcopy(self.base_profile)
        for key,var in self.limit_vars.items():
            v=optional_number(var.get())
            if v is None:profile['limits'].pop(key,None)
            else:profile['limits'][key]=v
        for key,var in self.requirement_vars.items():
            v=BOOL_CHOICES[var.get()]
            if v is None:profile['requirements'].pop(key,None)
            else:profile['requirements'][key]=v
        for key,var in self.threshold_vars.items():
            v=optional_number(var.get())
            if v is None:profile['minimum_distances_m'].pop(key,None)
            else:profile['minimum_distances_m'][key]=v
        a,b=optional_number(self.moisture_lo.get()),optional_number(self.moisture_hi.get())
        if (a is None)!=(b is None):raise EditorError('Задайте обе границы A1 или оставьте обе пустыми.')
        profile['moisture_range']=[a,b] if a is not None else None
        profile.update(root_protection=self.root_mode.get(),state_label=self.state_label.get().strip(),filters=deepcopy(self.filters))
        data.update(name=self.name.get().strip(),radius_m=self.radius.get().replace(',','.'),color=self.color.get(),
                    category=self.category.get(),symbol=self.symbol.get(),plant_id=self.plant_id,
                    template=profile if self.enabled.get() else None)
        value=ProjectType.from_dict(data);validate_type_reference(value,self.reference)
        return value

    def pick_plant(self):
        try:spec=self.gather()
        except EditorError as exc:messagebox.showerror('Шаблон',str(exc),parent=self);return
        def chosen(pid):self.plant_id=pid;self.plant_label.configure(text=self.plant_name())
        self.candidate_window=CandidateDialog(self,spec,self.reference,chosen,self.plant_id)

    def refresh_filters(self):
        self.filter_table.set_rows([(str(i),((self.reference.parameter(r['parameter_id']) or {}).get('label',r['parameter_id']),
            OP_LABELS[r['operator']],value_text(r['value'])),r['parameter_id']) for i,r in enumerate(self.filters)])

    def add_filter(self):
        def done(row):
            filters=self.filters+[row];self._validate_filters(filters);self.filters=filters;self.refresh_filters()
        FilterDialog(self,self.reference,done)

    def _validate_filters(self,filters):
        from .templates import validate_template
        profile=new_template();profile['filters']=filters;validate_template(profile,self.reference)

    def edit_filter(self):
        ids=self.filter_table.tree.selection()
        if not ids:return
        index=int(ids[0])
        def done(row):
            filters=deepcopy(self.filters);filters[index]=row;self._validate_filters(filters);self.filters=filters;self.refresh_filters()
        FilterDialog(self,self.reference,done,self.filters[index])

    def remove_filter(self):
        ids=self.filter_table.tree.selection()
        if ids:self.filters.pop(int(ids[0]));self.refresh_filters()

    def tab_changed(self,event=None):
        if self.tabs.select()==str(self.ready_tab):self.refresh_ready()

    def ready_selected(self,rid):
        r=self.ready_rows.get(rid)
        if r:self.ready_info.configure(text=(r['input'] or 'Геометрия участка')+' — '+r['note'])

    def refresh_ready(self):
        try:
            spec=self.gather()
            settings=self.session.check_settings if self.session else {'enabled':[]}
            rows=required_inputs(spec,self.reference.rules(),settings);self.ready_rows={r['rule_id']:r for r in rows}
            self.ready_table.set_rows([(r['rule_id'],(r['label'],value_text(r['value']), 'Готово' if r['status']=='READY' else 'Нужны сведения'),r['input'] or '') for r in rows])
        except EditorError as exc:self.ready_info.configure(text=str(exc))

    def accept(self):
        try:
            value=self.gather()
            if value.template and value.plant_id:
                matched=match_plant(value,value.plant_id,self.reference)
                if matched['status']!='PASS' and not messagebox.askyesno('Тип с неподтверждённым назначением',
                    'Назначенное растение не подтверждено по текущему шаблону. Сохранить как черновик? Проверка не будет считать его допустимым.',
                    parent=self,default='no'):return
            self.callback(value,self.all_overrides.get());self.destroy()
        except EditorError as exc:messagebox.showerror('Не удалось сохранить тип',str(exc),parent=self)

    def request_close(self):
        try:changed=asdict(self.gather())!=self.initial or self.all_overrides.get()
        except EditorError:changed=True
        if not changed:self.destroy();return
        choice=messagebox.askyesnocancel('Изменения типа','Сохранить изменения типа перед закрытием?',parent=self)
        if choice is True:self.accept()
        elif choice is False:self.destroy()
