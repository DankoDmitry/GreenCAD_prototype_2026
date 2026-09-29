"""Local Tk/ttk user interface. It calls editor commands; it contains no planting rules."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
import json
import logging
import platform
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog, colorchooser
from .catalog import ROOT, load_plants, load_object_types, read_json
from .errors import EditorError, UnitsRequired
from .models import ProjectType
from .dxf_io import load_dxf, export_dxf
from .project_io import load_project, save_project
from .session import EditorSession
from .view import MapCanvas
from .checks_ui import ValidationController
from .checks import report_envelope, save_report
from .admissibility_ui import AdmissibilityController
from .reference_ui import ReferenceController
from .generation_ui import GenerationController

LOG = logging.getLogger(__name__)


class PlantPicker(tk.Toplevel):
    def __init__(self, master, plants, callback, current=None):
        super().__init__(master)
        self.title("Растение из каталога — проверка отдельной кнопкой")
        self.geometry("860x530")
        self.transient(master)
        self.grab_set()
        self.callback,self.plants=callback,plants
        frame=ttk.Frame(self,padding=16);frame.pack(fill="both",expand=True)
        ttk.Label(frame,text="Поиск по названию или ID",style="Header.TLabel").pack(anchor="w")
        self.query=tk.StringVar()
        ent=ttk.Entry(frame,textvariable=self.query);ent.pack(fill="x",pady=(8,12));ent.focus_set()
        self.tree=ttk.Treeview(frame,columns=("name","group","id"),show="headings",selectmode="browse")
        for key,title,width in [("name","Исходное название",380),("group","Группа",120),("id","ID записи",220)]:
            self.tree.heading(key,text=title);self.tree.column(key,width=width)
        self.tree.pack(fill="both",expand=True)
        bar=ttk.Scrollbar(self.tree,orient="vertical",command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set);bar.pack(side="right",fill="y")
        self.query.trace_add("write",lambda *_:self.populate())
        self.tree.bind("<Double-1>",lambda _:self.accept())
        self.tree.bind("<Return>",lambda _:self.accept())
        buttons=ttk.Frame(frame);buttons.pack(fill="x",pady=(12,0))
        ttk.Button(buttons,text="Не задано / из типа",command=lambda:self.finish(None)).pack(side="left")
        ttk.Button(buttons,text="Выбрать",style="Accent.TButton",command=self.accept).pack(side="right")
        ttk.Button(buttons,text="Отмена",command=self.destroy).pack(side="right",padx=8)
        ttk.Label(frame,text="Каталог сохраняет исходные группы сортов и дубли. После назначения используйте «Проверить допуски».",
                  style="Muted.TLabel",wraplength=780).pack(anchor="w",pady=(8,0))
        self.populate()
        if current and self.tree.exists(current):
            self.tree.selection_set(current);self.tree.see(current)

    def populate(self):
        self.tree.delete(*self.tree.get_children())
        q=self.query.get().casefold().strip()
        for p in self.plants:
            if q in (p['name']+' '+p['id']).casefold():
                self.tree.insert("","end",iid=p['id'],values=(p['name'],p['group'],p['id']))

    def finish(self,pid):
        self.callback(pid);self.destroy()

    def accept(self):
        ids=self.tree.selection()
        if ids:self.finish(ids[0])


from .templates_ui import TypeDialog, CandidateDialog


class MappingDialog(tk.Toplevel):
    def __init__(self,master,session,object_types,callback):
        super().__init__(master)
        self.title("Явное соответствие слоёв и объектов")
        self.geometry("980x590");self.transient(master);self.grab_set()
        self.session=session;self.callback=callback;self.object_types=object_types
        self.mapping=json.loads(json.dumps(session.drawing.layer_mapping))
        frame=ttk.Frame(self,padding=16);frame.pack(fill="both",expand=True)
        ttk.Label(frame,text="Семантика не определяется по надписям и цветам",style="Header.TLabel").pack(anchor="w")
        ttk.Label(frame,text="Сопоставление действует только для геометрии без XDATA. XDATA имеет приоритет; текст остаётся оформлением.",
                  wraplength=920,style="Muted.TLabel").pack(anchor="w",pady=(4,12))
        self.tree=ttk.Treeview(frame,columns=('layer','count','type','role'),show='headings',height=13)
        for k,t,w in [('layer','Слой',280),('count','Объектов',80),('type','Тип',310),('role','Смысл геометрии',170)]:
            self.tree.heading(k,text=t);self.tree.column(k,width=w)
        self.tree.pack(fill="both",expand=True)
        self.tree.bind('<<TreeviewSelect>>',self.select)
        row=ttk.Frame(frame);row.pack(fill="x",pady=12)
        self.options=["Не классифицировано"]+[f"{x['label']}  [{k}]" for k,x in sorted(object_types.items())]
        self.ids=[None]+[k for k in sorted(object_types)]
        self.kind=ttk.Combobox(row,values=self.options,state='readonly',width=65);self.kind.pack(side='left');self.kind.current(0)
        self.role=ttk.Combobox(row,values=['AREA','BOUNDARY','OUTER_CONTOUR','EDGE','AXIS','CENTER','SYMBOL','CONTEXT'],state='readonly',width=18)
        self.role.pack(side='left',padx=8);self.role.set('CONTEXT')
        ttk.Button(row,text='Применить к слою',command=self.apply_row).pack(side='left')
        buttons=ttk.Frame(frame);buttons.pack(fill='x')
        ttk.Button(buttons,text='Импорт JSON…',command=self.import_json).pack(side='left')
        ttk.Button(buttons,text='Сохранить JSON…',command=self.export_json).pack(side='left',padx=8)
        ttk.Button(buttons,text='Готово',style='Accent.TButton',command=self.finish).pack(side='right')
        self.populate()

    def populate(self):
        current=self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for i,layer in enumerate(self.session.drawing.layers):
            name=layer['name'];spec=self.mapping.get(name,{})
            self.tree.insert('','end',iid=str(i),values=(name,layer['count'],spec.get('type_id','—'),spec.get('geometry_role','—')))
        if current and self.tree.exists(current[0]):self.tree.selection_set(current[0])

    def select(self,event=None):
        sel=self.tree.selection()
        if not sel:return
        name=self.tree.item(sel[0],'values')[0];spec=self.mapping.get(name,{})
        pid=spec.get('type_id')
        self.kind.current(self.ids.index(pid) if pid in self.ids else 0)
        self.role.set(spec.get('geometry_role','CONTEXT'))

    def apply_row(self):
        sel=self.tree.selection()
        if not sel:return
        name=self.tree.item(sel[0],'values')[0]
        pid=self.ids[self.kind.current()]
        if pid:self.mapping[name]={'type_id':pid,'geometry_role':self.role.get()}
        else:self.mapping.pop(name,None)
        self.populate()

    def import_json(self):
        path=filedialog.askopenfilename(parent=self,filetypes=[('Профиль слоёв','*.json')])
        if not path:return
        try:
            data=read_json(Path(path))
            if data.get('schema_version')!=1 or not isinstance(data.get('layers'),dict):
                raise EditorError('Ожидается schema_version=1 и объект layers.')
            for name,spec in data['layers'].items():
                if not isinstance(spec,dict) or spec.get('type_id') not in self.object_types:
                    raise EditorError(f'Неизвестный type_id для слоя {name}.')
                if spec.get('geometry_role') not in ['AREA','BOUNDARY','OUTER_CONTOUR','EDGE','AXIS','CENTER','SYMBOL','CONTEXT']:
                    raise EditorError(f'Неизвестная роль геометрии для {name}.')
            self.mapping=data['layers'];self.populate()
        except EditorError as exc:messagebox.showerror('Профиль',str(exc),parent=self)

    def export_json(self):
        path=filedialog.asksaveasfilename(parent=self,defaultextension='.json',initialfile='layer_mapping.json')
        if path:Path(path).write_text(json.dumps({'schema_version':1,'layers':self.mapping},ensure_ascii=False,indent=2),encoding='utf-8')

    def finish(self):
        self.callback(self.mapping);self.destroy()


class EditorApp:
    def __init__(self, root: tk.Tk):
        self.root=root
        self.root.title('GreenCAD • локальный DXF-редактор')
        self.root.geometry('1460x900');self.root.minsize(1120,730)
        self.session: EditorSession | None=None
        self.project_path:Path|None=None
        self.plants=load_plants();self.plant_names={p['id']:p['name'] for p in self.plants}
        self.object_types=load_object_types()
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='dxf-worker')
        self.busy=False;self._updating=False
        self._selected_feature=None;self._plant_override=None
        self.validation_controller = None
        self.admissibility_controller = None
        self.reference_controller = None
        self.generation_controller = None
        self._reference_id = None
        self._style();self._build()
        self.validation_controller = ValidationController(self)
        self.admissibility_controller = AdmissibilityController(self)
        self.reference_controller = ReferenceController(self)
        self.generation_controller = GenerationController(self)
        root.protocol('WM_DELETE_WINDOW',self.close)
        root.report_callback_exception=self.callback_error
        root.bind('<Control-o>',lambda e:self.open_dialog())
        root.bind('<Control-s>',lambda e:self.save())
        root.bind('<Control-Shift-S>',lambda e:self.save(save_as=True))
        root.bind('<Control-e>',lambda e:self.export())
        root.bind('<Control-z>',self.undo_shortcut)
        root.bind('<Control-y>',self.redo_shortcut)
        self.canvas.bind('<Delete>',lambda e:self.delete())
        self.canvas.bind('<Control-d>',lambda e:self.duplicate())
        self.canvas.bind('<Key-f>',lambda e:self.canvas.fit())
        self.canvas.bind('<Key-F>',lambda e:self.canvas.fit())
        self._status('Откройте DXF или тестовый участок. Исходные объекты доступны только для просмотра.')

    def _style(self):
        self.font='Segoe UI' if platform.system()=='Windows' else 'DejaVu Sans'
        self.root.option_add('*Font',(self.font,10))
        style=ttk.Style(self.root);style.theme_use('clam')
        style.configure('.',background='#F4F6F3',foreground='#253B30',font=(self.font,10))
        style.configure('TButton',padding=(10,7),background='#E6EDE7',borderwidth=0)
        style.map('TButton',background=[('active','#D7E3D8')])
        style.configure('Accent.TButton',background='#276348',foreground='white')
        style.map('Accent.TButton',background=[('active','#387958'),('disabled','#BDC9BE')])
        style.configure('Header.TLabel',font=(self.font,11,'bold'))
        style.configure('Muted.TLabel',foreground='#66786D',font=(self.font,9))
        style.configure('Treeview',background='white',fieldbackground='white',rowheight=28,borderwidth=0)
        style.configure('Treeview.Heading',background='#E7EEE7',font=(self.font,9,'bold'))
        style.map('Treeview',background=[('selected','#DAEBDD')],foreground=[('selected','#1C4631')])
        style.configure('TNotebook.Tab',padding=(12,7))
        style.configure('TLabelframe',padding=8)
        style.configure('TEntry',padding=5)

    def _build(self):
        header=tk.Frame(self.root,bg='#193F30',height=63);header.pack(fill='x');header.pack_propagate(False)
        tk.Label(header,text='GreenCAD',bg='#193F30',fg='white',font=(self.font,21,'bold')).pack(side='left',padx=20)
        tk.Label(header,text='ЛОКАЛЬНЫЙ РЕДАКТОР  /  DXF → ПРОЕКТ → DXF',bg='#193F30',fg='#C4DACE',font=(self.font,10)).pack(side='left',padx=12)
        tk.Label(header,text='0.6.1  •  2D',bg='#193F30',fg='#C4DACE',font=(self.font,10,'bold')).pack(side='right',padx=20)
        toolbar=ttk.Frame(self.root,padding=(12,8));toolbar.pack(fill='x')
        self.toolbuttons=[]
        for title,cmd,accent in [('Открыть…',self.open_dialog,False),('Демо',self.open_demo,False),('Сохранить проект',self.save,False),('Экспорт DXF',self.export,True),('↶ Отмена',self.undo,False),('↷ Повтор',self.redo,False),('Вписать',lambda:self.canvas.fit(),False),('Состав DXF',self.report,False)]:
            button=ttk.Button(toolbar,text=title,command=cmd,style='Accent.TButton' if accent else 'TButton');button.pack(side='left',padx=3);self.toolbuttons.append(button)
        body=ttk.Panedwindow(self.root,orient='horizontal');self.body=body;body.pack(fill='both',expand=True,padx=10,pady=10)
        left=ttk.Frame(body,width=255,padding=(6,0,8,0));body.add(left,weight=0)
        center=ttk.Frame(body);body.add(center,weight=1)
        right=ttk.Frame(body,width=295,padding=(10,0,6,0));body.add(right,weight=0)
        left.pack_propagate(False);right.pack_propagate(False)
        ttk.Label(left,text='ПРОЕКТНЫЕ ТИПЫ',style='Header.TLabel').pack(anchor='w',pady=(0,7))
        self.types_tree=ttk.Treeview(left,columns=('name','radius'),show='headings',height=5,selectmode='browse')
        self.types_tree.heading('name',text='Название');self.types_tree.heading('radius',text='R, м')
        self.types_tree.column('name',width=177,stretch=True);self.types_tree.column('radius',width=45,stretch=False)
        self.types_tree.pack(fill='x');self.types_tree.bind('<<TreeviewSelect>>',self.select_type)
        types_buttons=ttk.Frame(left);types_buttons.pack(fill='x',pady=7)
        ttk.Button(types_buttons,text='Настроить',command=self.edit_type).pack(side='left')
        ttk.Button(types_buttons,text='+ Тип',command=lambda:self.edit_type(new=True)).pack(side='left',padx=5)
        extra_type_buttons=ttk.Frame(left);extra_type_buttons.pack(fill='x',pady=(0,7))
        ttk.Button(extra_type_buttons,text='Копия типа',command=self.copy_type).pack(side='left')
        ttk.Button(extra_type_buttons,text='Удалить тип',command=self.delete_type).pack(side='left',padx=5)
        ttk.Label(left,text='Радиус — размер условного знака,\nне корней и не охранной зоны.',style='Muted.TLabel').pack(anchor='w',pady=(0,12))
        nb=ttk.Notebook(left);nb.pack(fill='both',expand=True)
        layer_tab=ttk.Frame(nb,padding=4);place_tab=ttk.Frame(nb,padding=4)
        nb.add(layer_tab,text='Слои');nb.add(place_tab,text='Посадки')
        self.layers_tree=ttk.Treeview(layer_tab,columns=('visible','name'),show='headings',height=12,selectmode='browse')
        self.layers_tree.heading('visible',text='Вид');self.layers_tree.heading('name',text='Слой')
        self.layers_tree.column('visible',width=37,stretch=False);self.layers_tree.column('name',width=175)
        self.layers_tree.pack(fill='both',expand=True);self.layers_tree.bind('<Double-1>',self.toggle_layer)
        ttk.Label(layer_tab,text='Двойной щелчок: скрыть / показать.',style='Muted.TLabel').pack(anchor='w',pady=6)
        ttk.Button(layer_tab,text='Соответствие слоёв…',command=self.mapping).pack(fill='x',pady=4)
        self.placements_tree=ttk.Treeview(place_tab,columns=('name','type'),show='headings',selectmode='browse')
        self.placements_tree.heading('name',text='Посадка');self.placements_tree.heading('type',text='Тип')
        self.placements_tree.column('name',width=160);self.placements_tree.column('type',width=50)
        self.placements_tree.pack(fill='both',expand=True);self.placements_tree.bind('<<TreeviewSelect>>',self.select_placement_list)
        tools=ttk.Frame(center,padding=(4,0,4,7));tools.pack(fill='x')
        self.mode=tk.StringVar(value='select')
        for text,value in [('Выбор / перенос','select'),('+ Посадка','add'),('Рука','pan')]:
            ttk.Radiobutton(tools,text=text,value=value,variable=self.mode,command=self.mode_changed).pack(side='left',padx=(0,10))
        ttk.Button(tools,text='−',width=3,command=lambda:self.canvas.zoom(1/1.3)).pack(side='right',padx=2)
        ttk.Button(tools,text='+',width=3,command=lambda:self.canvas.zoom(1.3)).pack(side='right',padx=2)
        self.canvas=MapCanvas(center,on_change=self.on_change,on_select=self.on_select,on_coordinates=self.coordinates)
        self.canvas.pack(fill='both',expand=True)
        viewtools=ttk.Frame(center,padding=(5,7));viewtools.pack(fill='x')
        self.grid_var=tk.BooleanVar(value=True);self.text_var=tk.BooleanVar(value=False);self.labels_var=tk.BooleanVar(value=True)
        ttk.Checkbutton(viewtools,text='Сетка',variable=self.grid_var,command=self.view_options).pack(side='left')
        ttk.Checkbutton(viewtools,text='Подписи DXF',variable=self.text_var,command=self.view_options).pack(side='left',padx=10)
        ttk.Checkbutton(viewtools,text='Метки посадок',variable=self.labels_var,command=self.view_options).pack(side='left')
        ttk.Label(viewtools,text='Привязка:').pack(side='right',padx=5)
        self.snap=ttk.Combobox(viewtools,values=['Нет','0.25 м','0.5 м','1 м','2 м'],state='readonly',width=8)
        self.snap.current(0);self.snap.pack(side='right');self.snap.bind('<<ComboboxSelected>>',lambda _:self.view_options())
        ttk.Label(center,text='Колесо — масштаб • ПКМ / средняя кнопка — сдвиг • Delete — удалить • Ctrl+D — копия',style='Muted.TLabel').pack(anchor='w',padx=5)
        ttk.Label(right,text='СВОЙСТВА',style='Header.TLabel').pack(anchor='w',pady=(0,8))
        self.selection_caption=ttk.Label(right,text='Выберите посадку или объект\nисходного плана.',wraplength=260)
        self.selection_caption.pack(anchor='w',pady=(0,12))
        self.editor=ttk.Frame(right);self.editor.pack(fill='x')
        self.editor.columnconfigure(1,weight=1)
        self.edit_type_var=tk.StringVar();self.x_var=tk.StringVar();self.y_var=tk.StringVar();self.label_var=tk.StringVar()
        self.root_var=tk.BooleanVar();self.lock_var=tk.BooleanVar()
        ttk.Label(self.editor,text='Тип').grid(row=0,column=0,sticky='w',pady=5)
        self.edit_type_combo=ttk.Combobox(self.editor,textvariable=self.edit_type_var,state='readonly',width=23)
        self.edit_type_combo.grid(row=0,column=1,sticky='ew',pady=5)
        for row,label,var in [(1,'X, м',self.x_var),(2,'Y, м',self.y_var),(3,'Метка',self.label_var)]:
            ttk.Label(self.editor,text=label).grid(row=row,column=0,sticky='w',pady=5)
            ttk.Entry(self.editor,textvariable=var,width=20).grid(row=row,column=1,sticky='ew',pady=5)
        ttk.Checkbutton(self.editor,text='Корнезащита (только флаг)',variable=self.root_var).grid(row=4,column=0,columnspan=2,sticky='w',pady=5)
        ttk.Checkbutton(self.editor,text='Закрепить положение',variable=self.lock_var).grid(row=5,column=0,columnspan=2,sticky='w',pady=5)
        self.plant_label=ttk.Label(self.editor,text='Растение не задано',wraplength=260)
        self.plant_label.grid(row=6,column=0,columnspan=2,sticky='w',pady=8)
        ttk.Button(self.editor,text='Выбрать растение…',command=self.pick_plant).grid(row=7,column=0,columnspan=2,sticky='ew',pady=4)
        ttk.Button(self.editor,text='Применить свойства',style='Accent.TButton',command=self.apply_properties).grid(row=8,column=0,columnspan=2,sticky='ew',pady=(9,6))
        buttons=ttk.Frame(self.editor);buttons.grid(row=9,column=0,columnspan=2,sticky='ew')
        ttk.Button(buttons,text='Копия',command=self.duplicate).pack(side='left',expand=True,fill='x')
        ttk.Button(buttons,text='Удалить',command=self.delete).pack(side='left',expand=True,fill='x',padx=(5,0))
        self.editor_separator = ttk.Separator(right)
        self.editor_separator.pack(fill='x',pady=14)
        ttk.Label(right,text='СВЕДЕНИЯ ОБ ОБЪЕКТЕ',style='Header.TLabel').pack(anchor='w',pady=(0,7))
        self.info=tk.Text(right,height=12,width=28,wrap='word',bg='#EDF2EC',fg='#465E4E',relief='flat',padx=9,pady=9,font=(self.font,9))
        self.info.pack(fill='both',expand=True)
        self.set_info('Исходная геометрия заблокирована.\n\nВыберите тип слева, включите «+ Посадка» и щёлкните по карте.\n\nНи одна посадка в этой версии не имеет подтверждённого нормативного статуса.')
        self.status_var=tk.StringVar();self.coord_var=tk.StringVar(value='X: —   Y: —')
        status=ttk.Frame(self.root,padding=(15,7));status.pack(fill='x')
        ttk.Label(status,textvariable=self.status_var,style='Muted.TLabel').pack(side='left',fill='x',expand=True)
        ttk.Label(status,textvariable=self.coord_var,style='Muted.TLabel').pack(side='right')
        self._enable_editor(False)

    def set_info(self,text):
        self.info.config(state='normal');self.info.delete('1.0','end');self.info.insert('1.0',text);self.info.config(state='disabled')

    def _enable_editor(self,enabled):
        for child in self.editor.winfo_children():
            if isinstance(child,(ttk.Entry,ttk.Button,ttk.Checkbutton,ttk.Combobox)):
                child.configure(state=('readonly' if isinstance(child,ttk.Combobox) else 'normal') if enabled else 'disabled')
            elif isinstance(child,ttk.Frame):
                for b in child.winfo_children():b.configure(state='normal' if enabled else 'disabled')

    def _status(self,text):self.status_var.set(text)

    def coordinates(self,x,y):self.coord_var.set(f'X: {x:.3f} м    Y: {y:.3f} м')

    def callback_error(self,kind,error,tb):
        LOG.error('Ошибка обработчика',exc_info=(kind,error,tb))
        messagebox.showerror('GreenCAD',str(error)+'\n\nПодробности: logs/editor.log',parent=self.root)

    def run_task(self,fn,on_done,label):
        if self.busy:return
        self.busy=True;self.canvas.editable=False;self._status(label)
        for b in self.toolbuttons:b.configure(state='disabled')
        # Tk variables from closed dialogs must not be finalized by cyclic GC
        # on the DXF worker. Collect UI cycles here, then defer automatic GC
        # until the worker is finished. All Tk calls remain on the main thread.
        import gc
        gc.collect()
        gc_was_enabled = gc.isenabled()
        gc.disable()
        future=self.executor.submit(fn)
        def poll():
            if not future.done():self.root.after(40,poll);return
            self.busy=False;self.canvas.editable=True
            if gc_was_enabled: gc.enable()
            gc.collect()
            for b in self.toolbuttons:b.configure(state='normal')
            try:on_done(future.result())
            except UnitsRequired as exc:
                self._status(str(exc))
                self._units_dialog()
            except Exception as exc:
                LOG.exception('Операция не выполнена')
                self._status('Операция не выполнена; исходный файл не изменён.')
                messagebox.showerror('GreenCAD',str(exc),parent=self.root)
        self.root.after(40,poll)

    def _units_dialog(self):
        path=getattr(self,'_pending_dxf',None)
        if not path:return
        value=simpledialog.askfloat('Единицы чертежа',
            'Сколько метров в одной единице DXF?\n\nМетры: 1; миллиметры: 0.001.\nМасштаб нельзя угадать по геометрии.',
            minvalue=1e-12,parent=self.root)
        if value is not None:self.open_path(path,confirm=False,metres_per_unit=value)

    def confirm_leave(self):
        if self.busy:return False
        if not self.session or not self.session.dirty:return True
        answer=messagebox.askyesnocancel('Несохранённый проект','Сохранить проект перед продолжением?\nЭкспорт DXF не заменяет сохранение рабочей сессии.',parent=self.root)
        if answer is None:return False
        return self.save() if answer else True

    def open_dialog(self):
        if not self.confirm_leave():return
        name=filedialog.askopenfilename(parent=self.root,filetypes=[('DXF или проект GreenCAD','*.dxf *.gcp'),('DXF','*.dxf'),('Проект GreenCAD','*.gcp')])
        if name:self.open_path(name,confirm=False)

    def open_demo(self):
        self.open_path(ROOT/'examples'/'demo_block.dxf')

    def open_path(self,path,confirm=True,metres_per_unit=None):
        if confirm and not self.confirm_leave():return
        path=Path(path)
        self._pending_dxf=path
        if path.suffix.lower()=='.gcp':
            self.run_task(lambda:load_project(path),lambda pair:self.adopt(pair[0],project_path=path,view=pair[1]),'Чтение проекта…')
        else:
            self.run_task(lambda:EditorSession(load_dxf(path,metres_per_unit=metres_per_unit)),lambda s:self.adopt(s),'Чтение DXF и подготовка карты…')

    def adopt(self,session,project_path=None,view=None):
        self.session=session;self.project_path=project_path
        self.sync_reference()
        self.canvas.set_session(session)
        self._selected_feature=None
        self.refresh_types();self.refresh_layers();self.refresh_placements()
        self.on_select(None,None)
        self.mode.set('select');self.mode_changed()
        if view:
            self.canvas.restore_view(view)
            self.grid_var.set(self.canvas.show_grid);self.text_var.set(self.canvas.show_annotations);self.labels_var.set(self.canvas.show_labels)
            snapvalues={0:'Нет',.25:'0.25 м',.5:'0.5 м',1:'1 м',2:'2 м'}
            self.snap.set(snapvalues.get(self.canvas.snap_m,'Нет'))
            self.refresh_layers()
        else:self.root.after(30,self.canvas.fit)
        self.on_change()
        if session.drawing.diagnostics:
            self._status(f"Открыт {session.drawing.source_name}. Замечаний: {len(session.drawing.diagnostics)} — «Состав DXF».")

    def sync_reference(self):
        if not self.session or self._reference_id == self.session.reference.id:return
        self._reference_id=self.session.reference.id
        self.plants=load_plants(self.session.reference)
        self.plant_names={p['id']:p['name'] for p in self.plants}
        self.object_types=load_object_types(self.session.reference)

    def show_reference(self):
        if not self.reference_controller or not self.session:return
        p=self.session.placements.get(self.session.selected_id)
        pid=(p.plant_id or self.session.types[p.type_id].plant_id) if p else None
        return self.reference_controller.show(pid)

    def refresh_types(self):
        if not self.session:return
        active=self.canvas.active_type
        self._updating=True
        self.types_tree.delete(*self.types_tree.get_children())
        for t in self.session.types.values():
            self.types_tree.insert('','end',iid=t.id,values=(f'{t.id}  {t.name}',f'{t.radius_m:g}'))
        if active not in self.session.types:active=next(iter(self.session.types))
        self.canvas.active_type=active
        self.types_tree.selection_set(active)
        self.edit_type_combo.configure(values=[f'{t.id} — {t.name}' for t in self.session.types.values()])
        self._updating=False

    def refresh_layers(self):
        self.layers_tree.delete(*self.layers_tree.get_children())
        if not self.session:return
        for i,layer in enumerate(self.session.drawing.layers):
            if any(f.layer==layer['name'] for f in self.session.drawing.features):
                self.layers_tree.insert('','end',iid=str(i),values=('—' if layer['name'] in self.canvas.hidden_layers else '✓',layer['name']))

    def refresh_placements(self):
        if not self.session:return
        self._updating=True
        self.placements_tree.delete(*self.placements_tree.get_children())
        for i,p in enumerate(self.session.placements.values(),1):
            self.placements_tree.insert('','end',iid=p.id,values=(p.label or f'Посадка {i}',p.type_id))
        if self.session.selected_id in self.session.placements:self.placements_tree.selection_set(self.session.selected_id)
        self._updating=False

    def on_change(self):
        if not self.session:return
        self.sync_reference()
        self.refresh_types()
        self.refresh_placements()
        self.root.title(f"{'* ' if self.session.dirty else ''}GreenCAD • {self.project_path.name if self.project_path else self.session.drawing.source_name}")
        self._status(f"{len(self.session.drawing.features)} исходных объектов  •  {len(self.session.placements)} посадок  •  координаты в метрах  •  допуски — по отдельной кнопке  •  замечаний: {len(self.session.drawing.diagnostics)}")
        self.canvas.request_render()
        if self.validation_controller:self.validation_controller.changed()
        if self.admissibility_controller:self.admissibility_controller.changed()
        if self.reference_controller:self.reference_controller.changed()
        if self.generation_controller:self.generation_controller.changed()

    def on_select(self,pid,fid):
        if not self.session:return
        self.editor.pack(fill="x", before=self.editor_separator)
        self._selected_feature=fid
        enabled=pid in self.session.placements
        self._enable_editor(enabled)
        if enabled:
            p=self.session.placements[pid];t=self.session.types[p.type_id]
            self.selection_caption.config(text='Посадка '+pid[:8]+'\nNOT_CHECKED — проверка ещё не запускалась')
            self.x_var.set(f'{p.x:.6f}');self.y_var.set(f'{p.y:.6f}');self.label_var.set(p.label)
            self.edit_type_var.set(f'{t.id} — {t.name}')
            self.root_var.set(p.root_protection);self.lock_var.set(p.locked)
            self._plant_override=p.plant_id;self.update_plant_label()
            self.set_info(f"ID: {p.id}\n\nТип: {p.type_id}\nСимвол: {t.symbol}\nРадиус знака: {t.radius_m:g} м\n\nИсточник: {'генератор, job '+str(p.properties['generation'].get('job_id',''))[:12] if isinstance(p.properties.get('generation'),dict) else 'ручная посадка'}.\nРазмеры условные, не нормативные.\n\nКорнезащита — только сохранённый признак.\nПроверка выбранных условий запускается отдельной кнопкой.")
        elif fid:
            f=next((x for x in self.session.drawing.features if x.id==fid),None)
            if not f:return
            label=self.object_types.get(f.type_id,{}).get('label',f.type_id or 'Не классифицировано')
            self.selection_caption.config(text=label+'\nИсходный объект — только чтение')
            text=f"ID: {f.id}\n\nСлой: {f.layer}\nDXF: {f.entity_type}\nHandle: {f.handle}\nТип: {f.type_id or 'не задан'}\nРоль геометрии: {f.geometry_role or 'не задана'}\nОпределено: {f.classification_source}\n\nСвойства:\n"+json.dumps(f.properties,ensure_ascii=False,indent=2)
            self.set_info(text)
        else:
            self.selection_caption.config(text='Выберите посадку или объект\nисходного плана.')
            self.set_info('Добавление: выберите тип слева, включите «+ Посадка» и щёлкните на карте.\n\nПеренос: режим «Выбор / перенос», затем перетащите посадку.\n\nИсходные объекты не редактируются. «Расставить типы…» создаёт предварительный вариант. Выбранные проверки запускаются кнопкой «Проверить допуски».')
        self.refresh_placements()
        if self.validation_controller:self.validation_controller.selected(pid)

    def update_plant_label(self):
        if not self.session or not self.session.selected_id:return
        p=self.session.placements[self.session.selected_id]
        default=self.session.types[p.type_id].plant_id
        if self._plant_override:
            text='Индивидуально: '+self.plant_names.get(self._plant_override,self._plant_override)
        else:text='Из проектного типа: '+self.plant_names.get(default,'не задано')
        self.plant_label.config(text=text)

    def select_type(self,event=None):
        if self._updating or not self.session:return
        ids=self.types_tree.selection()
        if ids:self.canvas.active_type=ids[0]

    def select_placement_list(self,event=None):
        if self._updating or not self.session:return
        ids=self.placements_tree.selection()
        if not ids or self.session.selected_id==ids[0]:return
        self.session.selected_id=ids[0];self.canvas.selected_feature=None
        self.on_select(ids[0],None);self.canvas.request_render()

    def mode_changed(self):
        self.canvas.cancel_drag();self.canvas.mode=self.mode.get()
        self.canvas.config(cursor='crosshair' if self.mode.get() in {'add','inspect'} else ('fleur' if self.mode.get()=='pan' else 'arrow'))

    def toggle_layer(self,event=None):
        ids=self.layers_tree.selection()
        if not ids:return
        name=self.layers_tree.item(ids[0],'values')[1]
        if name in self.canvas.hidden_layers:self.canvas.hidden_layers.remove(name)
        else:self.canvas.hidden_layers.add(name)
        self.refresh_layers();self.canvas.request_render()

    def view_options(self):
        self.canvas.show_grid=self.grid_var.get();self.canvas.show_annotations=self.text_var.get();self.canvas.show_labels=self.labels_var.get()
        self.canvas.snap_m={'Нет':0,'0.25 м':.25,'0.5 м':.5,'1 м':1.,'2 м':2.}.get(self.snap.get(),0)
        self.canvas.request_render()

    def pick_plant(self):
        if not self.session or not self.session.selected_id:return
        def selected(pid):self._plant_override=pid;self.update_plant_label()
        spec=self.session.types[self.session.placements[self.session.selected_id].type_id]
        if spec.template is not None:CandidateDialog(self.root,spec,self.session.reference,selected,self._plant_override)
        else:PlantPicker(self.root,self.plants,selected,self._plant_override)

    def apply_properties(self):
        if not self.session or not self.session.selected_id or self.busy:return
        try:
            ids=list(self.session.types)
            n=self.edit_type_combo.current()
            if n<0:raise EditorError('Выберите проектный тип.')
            previous=self.session.placements[self.session.selected_id].plant_id
            self.session.change(self.session.selected_id,type_id=ids[n],x=self.x_var.get().replace(',','.'),y=self.y_var.get().replace(',','.'),
                                label=self.label_var.get(),root_protection=self.root_var.get(),locked=self.lock_var.get(),plant_id=self._plant_override)
            self.on_change();self.on_select(self.session.selected_id,None)
            if previous!=self._plant_override and self.session.types[ids[n]].template is not None:
                self.root.after_idle(self.validation_controller.run)
        except EditorError as e:messagebox.showerror('Свойства',str(e),parent=self.root)

    def edit_type(self,new=False):
        if not self.session or self.busy:return
        if new:
            n=1
            while f'T{n}' in self.session.types:n+=1
            from .templates import new_template
            spec=ProjectType(f'T{n}','Новый тип',template=new_template())
        else:spec=self.session.types[self.canvas.active_type]
        def done(t,clear):
            self.session.set_type(t,clear_overrides=clear)
            self.canvas.active_type=t.id;self.refresh_types();self.on_change();self.on_select(self.session.selected_id,None)
            if t.template is not None and any(p.type_id==t.id and (p.plant_id or t.plant_id) for p in self.session.placements.values()):
                self.root.after_idle(self.validation_controller.run)
        return TypeDialog(self.root,spec,self.plants,done,session=self.session)

    def copy_type(self):
        if not self.session or self.busy:return
        try:
            spec=self.session.copy_type(self.canvas.active_type)
            self.canvas.active_type=spec.id;self.refresh_types();self.on_change()
        except EditorError as exc:messagebox.showerror('Копия типа',str(exc),parent=self.root)

    def delete_type(self):
        if not self.session or self.busy:return
        tid=self.canvas.active_type
        if not messagebox.askyesno('Удалить тип',f'Удалить проектный тип {tid}? Используемый посадками тип удалить нельзя.',parent=self.root):return
        try:
            self.session.delete_type(tid);self.canvas.active_type=next(iter(self.session.types))
            self.refresh_types();self.on_change()
        except EditorError as exc:messagebox.showerror('Удалить тип',str(exc),parent=self.root)

    def delete(self):
        if self.session and self.session.selected_id and not self.busy:
            self.session.delete(self.session.selected_id);self.on_change();self.on_select(None,None)

    def duplicate(self):
        if self.session and self.session.selected_id and not self.busy:
            p=self.session.duplicate(self.session.selected_id);self.on_change();self.on_select(p.id,None)

    def _is_text_focus(self):
        return isinstance(self.root.focus_get(),(tk.Text,tk.Entry,ttk.Entry,ttk.Combobox))

    def undo_shortcut(self,event):
        if not self._is_text_focus():self.undo();return 'break'

    def redo_shortcut(self,event):
        if not self._is_text_focus():self.redo();return 'break'

    def undo(self):
        if self.session and not self.busy and self.session.undo():
            self.canvas.cancel_drag();self.refresh_types();self.on_change();self.on_select(self.session.selected_id,None)

    def redo(self):
        if self.session and not self.busy and self.session.redo():
            self.canvas.cancel_drag();self.refresh_types();self.on_change();self.on_select(self.session.selected_id,None)

    def mapping(self):
        if not self.session or self.busy:return
        def done(m):
            self.session.set_mapping(m);self.canvas.selected_feature=None;self.on_change();self.on_select(None,None)
        MappingDialog(self.root,self.session,self.object_types,done)

    def save(self,save_as=False):
        if not self.session or self.busy:return False
        path=self.project_path
        if path is None or save_as:
            name=filedialog.asksaveasfilename(parent=self.root,defaultextension='.gcp',initialfile=Path(self.session.drawing.source_name).stem+'.gcp',filetypes=[('Проект GreenCAD','*.gcp')])
            if not name:return False
            path=Path(name)
        try:
            save_project(self.session,path,self.canvas.view_state());self.project_path=path
            self.on_change();self._status('Проект сохранён: '+str(path));return True
        except Exception as exc:
            LOG.exception('Сохранение проекта');messagebox.showerror('Сохранение',str(exc),parent=self.root);return False

    def export(self):
        if not self.session or self.busy:return
        name=filedialog.asksaveasfilename(parent=self.root,defaultextension='.dxf',initialfile=Path(self.session.drawing.source_name).stem+'_edited.dxf',filetypes=[('DXF','*.dxf')])
        if not name:return
        drawing=self.session.drawing
        from copy import deepcopy
        placements=deepcopy(list(self.session.placements.values()));types=deepcopy(self.session.types)
        checks_payload=report_envelope(self.session)
        def finished(report):
            from .dxf_io import sha256
            checks_payload['output_dxf'] = {'name':Path(name).name,'sha256':sha256(Path(name).read_bytes())}
            checks_path=Path(name).with_suffix('.checks.json')
            save_report(checks_payload,checks_path)
            report['selected_rules_report_state']=checks_payload['state']
            report['checks_sidecar']=checks_path.name
            report_path=Path(name).with_suffix('.export.json')
            try:report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            except OSError as e:LOG.warning('Отчёт экспорта не записан: %s',e)
            self._status(f"DXF сохранён; {report['placements']} посадок, исходные сущности проверены. Допуски: {checks_payload['state']} (см. .checks.json).")
            messagebox.showinfo('Экспорт завершён',f"Создан: {name}\n\nПосадок: {report['placements']}\nИсходные сущности и координаты проверены повторным чтением.\n\nДопуски: {checks_payload['state']}. Отчёт: {checks_path.name}\nЭто выбранные проверки, не комплексное нормативное заключение.",parent=self.root)
        self.run_task(lambda:export_dxf(drawing,placements,types,name),finished,'Запись и повторная проверка DXF…')

    def report(self):
        if not self.session:return
        d=self.session.drawing
        from collections import Counter
        data={'source':d.source_name,'sha256':d.source_sha256,'dxf_version':d.dxf_version,'metres_per_unit':d.metres_per_unit,
              'input_modelspace_count':d.modelspace_count,'editable_placements':len(self.session.placements),
              'classification':dict(Counter(f.classification_source for f in d.features)),
              'layers':d.layers,'diagnostics':d.diagnostics,
              'scope':'Редактор и генератор шаблонов. Текст не интерпретируется. Не все DXF-сущности поддержаны. Статус проверок — в .checks.json; это не полная нормативная экспертиза.',
              'selected_rules_validation':self.validation_controller.report_state() if self.validation_controller else 'NOT_CHECKED'}
        dlg=tk.Toplevel(self.root);dlg.title('Состав DXF и ограничения предпросмотра');dlg.geometry('880x650')
        text=tk.Text(dlg,wrap='word',font=('Consolas' if platform.system()=='Windows' else 'DejaVu Sans Mono',10))
        text.pack(fill='both',expand=True,padx=12,pady=12);text.insert('1.0',json.dumps(data,ensure_ascii=False,indent=2));text.config(state='disabled')

    def close(self):
        if self.busy:
            messagebox.showinfo('Выполняется операция','Дождитесь завершения операции. Расчёт можно остановить кнопкой «Стоп».',parent=self.root);return
        if self.confirm_leave():
            self.admissibility_controller.shutdown()
            self.generation_controller.shutdown()
            self.executor.shutdown(wait=False,cancel_futures=True)
            self.root.destroy()
