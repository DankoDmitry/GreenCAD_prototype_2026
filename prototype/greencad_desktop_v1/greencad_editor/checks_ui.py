"""Rule chooser and report widgets. No spatial or botanical calculations here."""
from __future__ import annotations
from copy import deepcopy
import json
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from .catalog import DATA, ROOT, read_json
from .checks import (load_rules, default_settings, validate_settings, fingerprint,
                     make_request, validate_plan, report_envelope, save_report,
                     STATUS_LABELS, RULE_LABELS, SOIL_KEYS)
from .errors import EditorError

SOIL_LABELS={'soil_drained':'Грунт дренирован','soil_loose':'Грунт рыхлый',
             'soil_saline':'Грунт засолён','soil_compacted':'Грунт уплотнён',
             'stagnant_water':'Есть застойное увлажнение'}
BOOLS={'Не задано':None,'Да':True,'Нет':False}


def number(text):
    return None if not text.strip() else float(text.replace(',','.'))


def details(row):
    lines=[f"Посадка: {row['label'] or row['id'][:8]}  |  {row['type_id']}",
           f"X={row['x']:.9g} м; Y={row['y']:.9g} м",f"Итог: {STATUS_LABELS[row['status']]}",
           'Проверен только выбранный набор условий.\n']
    for r in row['checks']:
        lines.extend([f"{STATUS_LABELS[r['status']]} — {r['label']} [{r['rule_id']}]",r['reason']])
        if r['actual'] is not None or r['required'] is not None:
            lines.append(f"Фактически: {r['actual']}; требуется: {r['required']} {r['unit']}")
        if r['object_ids']:lines.append('Объекты: '+', '.join(r['object_ids']))
        lines.extend('Источник: '+s.get('source_id','')+'; '+s.get('locator','') for s in r['sources'])
        if r['evidence']:lines.append('Данные: '+json.dumps(r['evidence'],ensure_ascii=False))
        lines.append('')
    return '\n'.join(lines)


class _ScrollableTab(ttk.Frame):
    """Keep long forms reachable without allowing them to hide dialog actions."""
    def __init__(self, master, padding=12):
        super().__init__(master)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        background = ttk.Style(self).lookup('TFrame', 'background') or '#F4F6F3'
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0,
                                background=background, width=1, height=1)
        self.canvas.grid(row=0, column=0, sticky='nsew')
        ybar = ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        xbar = ttk.Scrollbar(self, orient='horizontal', command=self.canvas.xview)
        ybar.grid(row=0, column=1, sticky='ns')
        xbar.grid(row=1, column=0, sticky='ew')
        self.canvas.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.content = ttk.Frame(self.canvas, padding=padding)
        self._window = self.canvas.create_window(0, 0, anchor='nw', window=self.content)
        self.content.bind('<Configure>', self._layout)
        self.canvas.bind('<Configure>', self._layout)

    def _layout(self, event=None):
        width = max(self.canvas.winfo_width(), self.content.winfo_reqwidth())
        if int(float(self.canvas.itemcget(self._window, 'width'))) != width:
            self.canvas.itemconfigure(self._window, width=width)
        self.canvas.configure(scrollregion=(0, 0, width, self.content.winfo_reqheight()))

    def scroll(self, event):
        # Scoped by RulePicker to this tab; no global bindings on the map.
        if isinstance(event.widget, ttk.Combobox):
            return
        if getattr(event, 'num', None) == 4:
            units = -3
        elif getattr(event, 'num', None) == 5:
            units = 3
        else:
            delta = getattr(event, 'delta', 0)
            if not delta:
                return
            units = -max(1, abs(int(delta)) // 120) * (1 if delta > 0 else -1) * 3
        self.canvas.yview_scroll(units, 'units')
        return 'break'


class RulePicker(tk.Toplevel):
    def __init__(self,master,session,callback):
        super().__init__(master)
        self.title('Допуски — выбранные проверки и параметры')
        self.geometry('960x730');self.minsize(850,620);self.transient(master);self.grab_set()
        self.protocol('WM_DELETE_WINDOW', self.request_close)
        self.bind('<Escape>', self.request_close)
        self.bind('<Control-Return>', self.accept_shortcut)
        self.session=session;self.callback=callback;self.rules=load_rules(session.reference)
        self.settings=deepcopy(session.check_settings)
        self.selected=set(self.settings['enabled'])
        self.facts=session.reference.facts()
        frame=ttk.Frame(self,padding=16);frame.pack(fill='both',expand=True)
        frame.columnconfigure(0, weight=1)
        # Only the notebook may shrink. Header/footer keep their requested height.
        frame.rowconfigure(2, weight=1)
        ttk.Label(frame,text='ЧТО ИМЕННО ПРОВЕРЯЕМ',style='Header.TLabel').grid(row=0,column=0,sticky='w')
        heading=ttk.Label(frame,text='Галочки включают условия. Начальные численные отступы — тестовые параметры, а не нормы ГОСТ/СП.\nНезаполненные значения дают «Недостаточно данных».',style='Muted.TLabel',wraplength=900)
        heading.grid(row=1,column=0,sticky='ew',pady=(5,10))
        frame.bind('<Configure>',lambda e:heading.configure(wraplength=max(200,e.width-32)))
        self.tabs=tabs=ttk.Notebook(frame)
        tabs.grid(row=2,column=0,sticky='nsew')
        rules_tab=ttk.Frame(tabs,padding=10)
        rules_tab.columnconfigure(0,weight=1);rules_tab.rowconfigure(0,weight=1)
        self.params_tab=_ScrollableTab(tabs)
        self.absent_tab=_ScrollableTab(tabs)
        params=self.params_tab.content;absent_tab=self.absent_tab.content
        registry_tab=ttk.Frame(tabs,padding=12)
        for tab,title in [(rules_tab,'Проверки'),(self.params_tab,'Параметры'),(self.absent_tab,'Отсутствующие объекты'),(registry_tab,'Не подключено')]:tabs.add(tab,text=title)
        for sequence in ('<MouseWheel>','<Button-4>','<Button-5>'):
            self.bind(sequence,self.scroll_form)
        self.tree=ttk.Treeview(rules_tab,columns=('on','name','kind'),show='headings',height=11,selectmode='browse')
        for key,title,width in [('on','Вкл.',50),('name','Проверка',400),('kind','Основание',310)]:
            self.tree.heading(key,text=title);self.tree.column(key,width=width,stretch=key!='on')
        self.tree.grid(row=0,column=0,sticky='nsew')
        scrollbar=ttk.Scrollbar(self.tree,orient='vertical',command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set);scrollbar.pack(side='right',fill='y')
        self.tree.bind('<Button-1>',self.click);self.tree.bind('<space>',self.toggle_selected)
        self.tree.bind('<<TreeviewSelect>>',self.show_rule)
        self.rule_info=tk.Text(rules_tab,height=4,wrap='word',bg='#EDF2EC',relief='flat',padx=8,pady=8)
        self.rule_info.grid(row=1,column=0,sticky='ew',pady=(10,6))
        self.rule_info.configure(state='disabled')
        row=ttk.Frame(rules_tab);row.grid(row=2,column=0,sticky='ew')
        ttk.Button(row,text='Начальный набор',command=self.use_defaults).pack(side='left')
        ttk.Button(row,text='Все реализованные',command=self.select_all).pack(side='left',padx=8)
        ttk.Button(row,text='Снять всё',command=self.select_none).pack(side='left')
        self.populate()
        self.threshold_vars={}
        left=ttk.Frame(params);left.pack(side='left',fill='both',expand=True,padx=(0,16))
        right=ttk.Frame(params);right.pack(side='left',fill='both',expand=True)
        ttk.Label(left,text='Проектные минимумы, метры',style='Header.TLabel').pack(anchor='w')
        for r in self.rules:
            if 'threshold_m' not in r:continue
            row=ttk.Frame(left);row.pack(fill='x',pady=4)
            ttk.Label(row,text=r['label'],wraplength=275).pack(side='left')
            var=tk.StringVar(value=str(self.settings['thresholds_m'].get(r['id'],r['threshold_m'])))
            self.threshold_vars[r['id']]=var
            ttk.Entry(row,textvariable=var,width=8).pack(side='right')
        ttk.Separator(left).pack(fill='x',pady=12)
        ttk.Label(left,text='Среда: сценарий для всего участка',style='Header.TLabel').pack(anchor='w')
        row=ttk.Frame(left);row.pack(fill='x',pady=5)
        ttk.Label(row,text='Влажность A1, шкала 0–1').pack(side='left')
        self.a1=tk.StringVar(value='' if self.settings['site'].get('A1') is None else str(self.settings['site']['A1']))
        ttk.Entry(row,textvariable=self.a1,width=8).pack(side='right')
        self.bool_vars={}
        for key in SOIL_KEYS:
            row=ttk.Frame(left);row.pack(fill='x',pady=4)
            ttk.Label(row,text=SOIL_LABELS[key]).pack(side='left')
            val=self.settings['site'].get(key)
            var=tk.StringVar(value='Не задано' if val is None else ('Да' if val else 'Нет'))
            self.bool_vars[key]=var
            ttk.Combobox(row,textvariable=var,values=list(BOOLS),state='readonly',width=12).pack(side='right')
        ttk.Label(right,text='Категория территории',style='Header.TLabel').pack(anchor='w')
        self.territories=[None]+[t['id'] for t in self.facts['territories']]
        labels=['Не задано']+[t['label'] for t in self.facts['territories']]
        self.territory=ttk.Combobox(right,values=labels,state='readonly',width=37)
        self.territory.pack(fill='x',pady=7);self.territory.current(self.territories.index(self.settings['site'].get('territory')))
        ttk.Label(right,text='Подтверждена обычная территория\nбез особого режима?',wraplength=320).pack(anchor='w',pady=(8,4))
        val=self.settings['site'].get('ordinary_territory')
        self.ordinary=tk.StringVar(value='Не задано' if val is None else ('Да' if val else 'Нет'))
        ttk.Combobox(right,textvariable=self.ordinary,values=list(BOOLS),state='readonly',width=16).pack(anchor='w')
        ttk.Separator(right).pack(fill='x',pady=16)
        ttk.Label(right,text='Диапазон A1 проектного типа',style='Header.TLabel').pack(anchor='w')
        ttk.Label(right,text='Задаётся вручную. Не берётся из размера знака или названия растения.',style='Muted.TLabel',wraplength=330).pack(anchor='w',pady=5)
        self.type_ids=list(session.types)
        self.type_combo=ttk.Combobox(right,values=[f'{k} — {session.types[k].name}' for k in self.type_ids],state='readonly')
        self.type_combo.pack(fill='x',pady=5);self.type_combo.bind('<<ComboboxSelected>>',self.load_range)
        ranges=ttk.Frame(right);ranges.pack(fill='x',pady=5)
        self.lo=tk.StringVar();self.hi=tk.StringVar()
        ttk.Label(ranges,text='От').pack(side='left');ttk.Entry(ranges,textvariable=self.lo,width=8).pack(side='left',padx=6)
        ttk.Label(ranges,text='до').pack(side='left');ttk.Entry(ranges,textvariable=self.hi,width=8).pack(side='left',padx=6)
        ttk.Button(right,text='Записать диапазон типа',command=self.store_range_dialog).pack(anchor='w',pady=4)
        ttk.Label(right,text='Обе границы пусты — диапазон не задан.\nПеред выбором другого типа запишите правку.',style='Muted.TLabel',wraplength=330).pack(anchor='w',pady=5)
        if self.type_ids:self.type_combo.current(0);self.load_range()
        ttk.Label(absent_tab,text='Пустой слой не доказывает отсутствие объекта. Здесь можно явно подтвердить отсутствие категории.\nЕсли объект всё же присутствует, проверка обнаружит противоречие.',wraplength=850).pack(anchor='w',pady=(0,12))
        names={k:v['label'] for k,v in session.reference.objects().items()}
        keys=sorted({k for r in self.rules for k in r.get('object_types',[])})
        self.absent_vars={}
        for k in keys:
            var=tk.BooleanVar(value=k in self.settings['confirmed_absent']);self.absent_vars[k]=var
            ttk.Checkbutton(absent_tab,text=names.get(k,k)+'  ['+k+']',variable=var).pack(anchor='w',pady=3)
        used={rid for r in self.rules for rid in r.get('registry_ids',[])}
        pending={'rules':[r for r in session.reference.table('rules') if r['id'] not in used]}
        ttk.Label(registry_tab,text='Эти записи сохранены из реестра v1, но пока не имеют готовой реализации и/или подтверждённого профиля.\nОни НЕ включаются кнопкой «Все реализованные» и НЕ считаются проверенными.',wraplength=850).pack(anchor='w',pady=(0,12))
        text=tk.Text(registry_tab,wrap='word',bg='#EDF2EC',relief='flat',padx=8,pady=8)
        text.pack(fill='both',expand=True)
        text.insert('1.0','\n\n'.join(f"{r['id']} — {r['label']}\n{', '.join(r['source_ids'])}; {r['locator']}" for r in pending['rules']));text.config(state='disabled')
        self.footer=ttk.Frame(frame)
        self.footer.grid(row=3,column=0,sticky='ew',pady=(12,0))
        self.footer.columnconfigure(0,weight=1)
        hint=ttk.Label(self.footer,text='«Готово» применяет выбор к проекту.\nНа диск: «Сохранить проект» (Ctrl+S).',style='Muted.TLabel',wraplength=500)
        hint.grid(row=0,column=0,sticky='w',padx=(0,12))
        self.cancel_button=ttk.Button(self.footer,text='Отмена',command=self.destroy)
        self.cancel_button.grid(row=0,column=1,padx=(0,8))
        self.done_button=ttk.Button(self.footer,text='Готово',style='Accent.TButton',command=self.accept)
        self.done_button.grid(row=0,column=2,sticky='e')
        self.footer.bind('<Configure>',lambda e:hint.configure(wraplength=max(180,e.width-self.cancel_button.winfo_reqwidth()-self.done_button.winfo_reqwidth()-36)))
        # Compare normalized form values, not raw defaults (missing threshold keys).
        self._initial_values=self.collect_settings()

    def scroll_form(self,event):
        selected=self.tabs.select()
        for tab in (self.params_tab,self.absent_tab):
            if selected==str(tab) and str(event.widget).startswith(str(tab)+'.'):
                return tab.scroll(event)

    def accept_shortcut(self,event=None):
        self.accept()
        return 'break'

    def request_close(self,event=None):
        try:
            changed=self.collect_settings()!=self._initial_values
        except (ValueError,EditorError):
            changed=True
        if not changed:
            self.destroy()
            return 'break'
        answer=messagebox.askyesnocancel(
            'Неприменённые настройки',
            'Применить выбранные проверки и параметры к текущему проекту?\n\n'
            'Да — применить и закрыть. Нет — закрыть без изменений.\n'
            'Отмена — вернуться к настройкам.\n\n'
            'Для сохранения на диск затем сохраните проект (.gcp).',parent=self)
        if answer is True:
            self.accept()
        elif answer is False:
            self.destroy()
        return 'break'

    def populate(self):
        old=self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for r in self.rules:
            self.tree.insert('','end',iid=r['id'],values=('☑' if r['id'] in self.selected else '☐',r['label'],RULE_LABELS[r['kind']]))
        if old and self.tree.exists(old[0]):self.tree.selection_set(old[0])

    def click(self,event):
        if self.tree.identify_column(event.x)=='#1':
            rid=self.tree.identify_row(event.y)
            if rid:self.toggle(rid)

    def toggle_selected(self,event=None):
        sel=self.tree.selection()
        if sel:self.toggle(sel[0])
        return 'break'

    def toggle(self,rid):
        if rid in self.selected:self.selected.remove(rid)
        else:self.selected.add(rid)
        self.populate();self.tree.selection_set(rid)

    def use_defaults(self):self.selected=set(default_settings()['enabled']);self.populate()
    def select_all(self):self.selected={r['id'] for r in self.rules};self.populate()
    def select_none(self):self.selected=set();self.populate()

    def show_rule(self,event=None):
        sel=self.tree.selection()
        if not sel:return
        r=next(x for x in self.rules if x['id']==sel[0])
        self.rule_info.config(state='normal');self.rule_info.delete('1.0','end')
        self.rule_info.insert('1.0',r['description']+'\n\n'+r['id']+'\n'+
                              '\n'.join(x['source_id']+'; '+x['locator'] for x in r['provenance']))
        self.rule_info.config(state='disabled')

    def load_range(self,event=None):
        idx=self.type_combo.current()
        if idx<0:return
        pair=self.settings['type_ranges'].get(self.type_ids[idx])
        self.lo.set('' if pair is None else str(pair[0]));self.hi.set('' if pair is None else str(pair[1]))

    def _write_range(self,settings):
        idx=self.type_combo.current()
        if idx<0:return
        key=self.type_ids[idx]
        lo,hi=number(self.lo.get()),number(self.hi.get())
        if lo is None and hi is None:settings['type_ranges'].pop(key,None)
        elif lo is None or hi is None:raise EditorError('Задайте обе границы диапазона A1.')
        else:settings['type_ranges'][key]=[lo,hi]

    def store_range(self):
        draft=deepcopy(self.settings)
        self._write_range(draft)
        self.settings=validate_settings(draft, self.session.reference)

    def store_range_dialog(self):
        try:self.store_range()
        except (ValueError,EditorError) as e:messagebox.showerror('Диапазон',str(e),parent=self)

    def collect_settings(self):
        """Read a draft without changing the project or partially committing input."""
        draft=deepcopy(self.settings)
        self._write_range(draft)
        # Retain unknown registered IDs, so this dialog never silently disables them.
        known={r['id'] for r in self.rules}
        draft['enabled']=[r['id'] for r in self.rules if r['id'] in self.selected]
        draft['enabled'].extend(sorted(self.selected-known))
        draft['thresholds_m'].update({k:number(v.get()) for k,v in self.threshold_vars.items()})
        site=draft['site'];site['A1']=number(self.a1.get())
        site.update({k:BOOLS[v.get()] for k,v in self.bool_vars.items()})
        site['ordinary_territory']=BOOLS[self.ordinary.get()]
        idx=self.territory.current()
        if idx<0:raise EditorError('Выберите категорию территории или «Не задано».')
        site['territory']=self.territories[idx]
        draft['confirmed_absent']=[k for k,v in self.absent_vars.items() if v.get()]
        draft['confirmed_absent'].extend(k for k in self.settings['confirmed_absent'] if k not in self.absent_vars)
        return validate_settings(draft, self.session.reference)

    def accept(self):
        try:
            settings=self.collect_settings()
            if settings!=self._initial_values:
                self.callback(settings)
            self.destroy()
            return True
        except (ValueError,EditorError) as exc:
            messagebox.showerror('Параметры проверок',str(exc),parent=self)
            return False


class ReportWindow(tk.Toplevel):
    def __init__(self,controller):
        app=controller.app
        super().__init__(app.root)
        self.controller=controller
        self.title('Проверка допусков — отчёт по посадкам');self.geometry('1050x760')
        self.minsize(800,580);self.transient(app.root)
        frame=ttk.Frame(self,padding=16);frame.pack(fill='both',expand=True)
        self.caption=ttk.Label(frame,text='',style='Header.TLabel',wraplength=970);self.caption.pack(anchor='w',pady=(0,10))
        top=ttk.Frame(frame);top.pack(fill='x',pady=(0,8))
        ttk.Label(top,text='Фильтр:').pack(side='left')
        self.filter=tk.StringVar(value='Все')
        combo=ttk.Combobox(top,textvariable=self.filter,values=['Все',*STATUS_LABELS.values()],state='readonly',width=27)
        combo.pack(side='left',padx=8);combo.bind('<<ComboboxSelected>>',lambda _:self.refresh())
        ttk.Button(top,text='Сохранить JSON…',command=controller.save).pack(side='right')
        self.tree=ttk.Treeview(frame,columns=('name','type','status','fail','unknown'),show='headings',height=8,selectmode='browse')
        for k,t,w in [('name','Посадка',230),('type','Тип',90),('status','Результат',220),('fail','Нарушений',100),('unknown','Нет данных / ошибки',190)]:
            self.tree.heading(k,text=t);self.tree.column(k,width=w)
        self.tree.pack(fill='both',expand=True)
        self.tree.bind('<<TreeviewSelect>>',self.select)
        bar=ttk.Scrollbar(self.tree,orient='vertical',command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set);bar.pack(side='right',fill='y')
        self.text=tk.Text(frame,height=17,wrap='word',bg='#EDF2EC',fg='#314F3C',relief='flat',padx=12,pady=12)
        self.text.pack(fill='both',expand=True,pady=(12,0))
        self.refresh()

    def refresh(self):
        state=self.controller.report_state()
        self.caption.config(text=self.controller.summary())
        self.tree.delete(*self.tree.get_children())
        report=self.controller.app.session.validation_report if self.controller.app.session else None
        if not report:
            self.text.config(state='normal');self.text.delete('1.0','end');self.text.insert('1.0','Текущий проект ещё не проверен.');self.text.config(state='disabled')
            return
        for n,row in enumerate(report['placements'],1):
            if self.filter.get() not in ('Все',STATUS_LABELS[row['status']]):continue
            counts={s:sum(c['status']==s for c in row['checks']) for s in ('FAIL','UNKNOWN','ERROR')}
            self.tree.insert('','end',iid=row['id'],values=(row['label'] or f'Посадка {n} [{row["id"][:8]}]',row['type_id'],STATUS_LABELS[row['status']],counts['FAIL'],counts['UNKNOWN']+counts['ERROR']))
        self.text.config(state='normal');self.text.delete('1.0','end')
        self.text.insert('1.0','ОТЧЁТ УСТАРЕЛ. Показаны результаты старого снимка. Нажмите «Проверить допуски».\n' if state=='STALE' else 'Выберите посадку: ниже появятся все результаты, а не только первое нарушение.\n')
        self.text.config(state='disabled')

    def select(self,event=None):
        sel=self.tree.selection()
        if not sel:return
        app=self.controller.app;report=app.session.validation_report
        row=next((r for r in report['placements'] if r['id']==sel[0]),None)
        if row is None:return
        self.text.config(state='normal');self.text.delete('1.0','end')
        self.text.insert('1.0',('УСТАРЕВШИЙ СНИМОК\n\n' if self.controller.report_state()=='STALE' else '')+details(row))
        self.text.config(state='disabled')
        if row['id'] in app.session.placements:
            app.session.selected_id=row['id'];app.canvas.selected_feature=None
            app.on_select(row['id'],None);app.canvas.request_render()


class ValidationController:
    def __init__(self,app):
        self.app=app;self.window=None
        row=ttk.Frame(app.root,padding=(15,6))
        row.pack(fill='x',before=app.body)
        self.check_button=ttk.Button(row,text='Проверить допуски',style='Accent.TButton',command=self.run)
        self.check_button.pack(side='left')
        for text,command in [('Выбрать проверки…',self.choose),('Отчёт…',self.show)]:
            button=ttk.Button(row,text=text,command=command);button.pack(side='left',padx=5);app.toolbuttons.append(button)
        app.toolbuttons.append(self.check_button)
        self.status=tk.StringVar(value='Проверка ещё не запускалась')
        ttk.Label(row,textvariable=self.status,style='Muted.TLabel',wraplength=600).pack(side='left',padx=15)

    def report_state(self):
        session=self.app.session
        if not session or session.validation_report is None:return 'NOT_CHECKED'
        return 'CURRENT' if session.validation_report['fingerprint']==fingerprint(session) else 'STALE'

    def summary(self):
        session=self.app.session
        if not session:return 'Откройте участок.'
        n=len(session.check_settings['enabled'])
        state=self.report_state()
        if state=='NOT_CHECKED':return f'Выбрано проверок: {n}. Ещё не проверено.'
        if state=='STALE':return 'Отчёт устарел — повторите проверку.'
        r=session.validation_report;c=r['counts']
        if not r['placements']:return 'План пуст: посадок для проверки нет.'
        return f"Выбранный набор: прошло {c.get('PASS',0)} · нарушено {c.get('FAIL',0)} · нет данных {c.get('UNKNOWN',0)} · ошибки {c.get('ERROR',0)} · рекомендации {c.get('WARNING',0)} · не проверено {c.get('NOT_CHECKED',0)}"

    def changed(self):
        app=self.app
        state=self.report_state()
        self.status.set(self.summary())
        if app.session and state=='CURRENT':
            app.canvas.validation_statuses={r['id']:r['status'] for r in app.session.validation_report['placements']}
        else:app.canvas.validation_statuses={}
        app.canvas.request_render()
        if self.window and self.window.winfo_exists():self.window.refresh()

    def selected(self,pid):
        app=self.app
        if not app.session or pid not in app.session.placements:return
        state=self.report_state();row=None
        if app.session.validation_report:
            row=next((r for r in app.session.validation_report['placements'] if r['id']==pid),None)
        label='Не проверено' if row is None else ('Устарело — проверить заново' if state=='STALE' else STATUS_LABELS[row['status']])
        app.selection_caption.config(text='Посадка '+pid[:8]+'\n'+label)
        if row is not None:
            app.set_info(('УСТАРЕВШИЙ ОТЧЁТ\n\n' if state=='STALE' else '')+details(row))

    def choose(self):
        if not self.app.session or self.app.busy:return
        def done(settings):
            self.app.session.set_check_settings(settings)
            self.app.on_change();self.app.on_select(self.app.session.selected_id,None)
        RulePicker(self.app.root,self.app.session,done)

    def run(self):
        app=self.app
        if not app.session or app.busy:return
        app.canvas.cancel_drag()
        request=make_request(app.session)
        session=app.session
        def finished(report):
            if app.session is not session:return
            session.validation_report=report
            self.changed();app.on_select(session.selected_id,None)
            app._status(self.summary());self.show()
        app.run_task(lambda:validate_plan(request),finished,'Проверка выбранных условий для всех посадок…')

    def show(self):
        if not self.app.session:return
        if self.app.session.validation_report is None:
            messagebox.showinfo('Допуски','Сначала нажмите «Проверить допуски».',parent=self.app.root);return
        if self.window and self.window.winfo_exists():self.window.refresh();self.window.lift()
        else:self.window=ReportWindow(self)

    def save(self):
        if not self.app.session:return
        filename=filedialog.asksaveasfilename(parent=self.app.root,defaultextension='.json',initialfile='planting_checks.json',filetypes=[('Отчёт JSON','*.json')])
        if filename:save_report(report_envelope(self.app.session),filename)
