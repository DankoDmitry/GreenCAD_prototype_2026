"""Admissibility overlay/controller. All rule execution remains outside Tk.

The overlay is a derived, immutable snapshot, never added to the placement list
or to the source DXF. Plotting a cell is not approval of its entire square.
"""
from __future__ import annotations

from dataclasses import asdict
import queue
from threading import Event
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from .admissibility import (MapOptions, TOTAL, MAX_CELLS, build_map,
                            make_map_request, map_fingerprint)
from .checks import STATUS_LABELS, load_rules, save_report
from .checks_ui import _ScrollableTab, details
from .errors import EditorError

# View-only colours; never consumed by the geometry/rule engine.
COLOURS = {'PASS': '#53A76F', 'FAIL': '#D75A50', 'UNKNOWN': '#E6BB56',
           'ERROR': '#9764AC', 'NOT_CHECKED': '#A2AAA6',
           'NOT_APPLICABLE': '#A2AAA6', 'WARNING': '#DF9744'}


class MapOptionsDialog(tk.Toplevel):
    def __init__(self, controller):
        app = controller.app
        super().__init__(app.root)
        self.controller, self.app = controller, app
        self.title('Карта допустимости — тип посадки и разрешение')
        self.geometry('800x570')
        self.minsize(640, 430)
        self.transient(app.root)
        self.grab_set()
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        ttk.Label(frame, text='ГДЕ ПОДХОДИТ ВЫБРАННАЯ ПОСАДКА',
                  style='Header.TLabel').grid(row=0, column=0, sticky='w', pady=(0, 8))
        scroll = _ScrollableTab(frame)
        scroll.grid(row=1, column=0, sticky='nsew')
        body = scroll.content
        self.type_ids = list(app.session.types)
        old = controller.options
        tid = old.type_id if old and old.type_id in self.type_ids else app.canvas.active_type
        if tid not in self.type_ids:
            tid = self.type_ids[0]
        ttk.Label(body, text='Проектный тип').pack(anchor='w')
        self.type_combo = ttk.Combobox(body, state='readonly',
            values=[f'{t.id} — {t.name}' for t in app.session.types.values()])
        self.type_combo.pack(fill='x', pady=(4, 10))
        self.type_combo.current(self.type_ids.index(tid))
        self.type_combo.bind('<<ComboboxSelected>>', self.update_description)
        self.template_only=tk.BooleanVar(value=bool(old and old.template_only))
        ttk.Checkbutton(body,text='Проверять только расчётный шаблон, без назначенного растения',
            variable=self.template_only,command=self.update_description).pack(anchor='w',pady=(0,8))
        self.override = tk.BooleanVar(value=bool(old and old.plant_id))
        ttk.Checkbutton(body, text='Выбрать конкретное растение только для этой карты',
            variable=self.override, command=self.update_description).pack(anchor='w')
        self.plant_ids = [None] + [p['id'] for p in app.plants]
        self.plant_combo = ttk.Combobox(body, state='disabled',
            values=['Не задано'] + [p['name']+' ['+p['id']+']' for p in app.plants])
        self.plant_combo.pack(fill='x', pady=(5, 5))
        self.plant_combo.current(self.plant_ids.index(old.plant_id) if old and old.plant_id in self.plant_ids else 0)
        self.description = ttk.Label(body, style='Muted.TLabel', wraplength=670)
        self.description.pack(anchor='w', pady=(0, 10))
        profile=app.session.types[tid].template
        self.root_protection = tk.BooleanVar(value=old.root_protection if old else bool(profile and profile['root_protection']=='REQUIRED'))
        ttk.Checkbutton(body, text='Корнезащита (порог сама по себе не уменьшает)',
                        variable=self.root_protection).pack(anchor='w')
        row = ttk.Frame(body)
        row.pack(fill='x', pady=12)
        ttk.Label(row, text='Шаг отображения, м').pack(side='left')
        self.step = tk.StringVar(value=str(old.step_m if old else 1.0))
        ttk.Entry(row, textvariable=self.step, width=12).pack(side='left', padx=12)
        ttk.Label(body, text=f'Не более {MAX_CELLS:,} клеток. В каждой проверяется только центр. '
            'Перетаскивание посадок остаётся свободным; это не сетка авторасстановки.',
            style='Muted.TLabel', wraplength=670).pack(anchor='w', pady=(0, 10))
        rules = {r['id']: r for r in load_rules(app.session.reference)}
        enabled = app.session.check_settings['enabled']
        static = [rules.get(rid, {'label': rid})['label'] for rid in enabled
                  if not (rid in rules and (rules[rid].get('evaluation_scope') == 'PLAN' or rules[rid]['operator'] == 'spacing'))]
        ttk.Label(body, text='Выбранные проверки участка: '+('; '.join(static) or 'нет'),
                  wraplength=670).pack(anchor='w', pady=6)
        ttk.Label(body, text='Состав меняется в «Выбрать проверки…». После построения можно '
            'переключать итог и отдельные правила без повторного расчёта. '
            'Между новыми посадками расстояния здесь не проверяются. '
            'Начальные численные отступы — тестовые, а не нормы.',
            style='Muted.TLabel', wraplength=670).pack(anchor='w', pady=5)
        footer = ttk.Frame(frame)
        footer.grid(row=2, column=0, sticky='ew', pady=(10, 0))
        self.build_button = ttk.Button(footer, text='Построить карту',
                                       style='Accent.TButton', command=self.accept)
        self.build_button.pack(side='right')
        self.cancel_button = ttk.Button(footer, text='Отмена', command=self.destroy)
        self.cancel_button.pack(side='right', padx=8)
        self.bind('<Escape>', lambda e: self.destroy())
        for event in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind(event, scroll.scroll)
        self.update_description()

    def update_description(self, event=None):
        if self.template_only.get():self.override.set(False)
        override = self.override.get()
        self.plant_combo.configure(state='readonly' if override else 'disabled')
        tid = self.type_ids[self.type_combo.current()]
        pid = self.app.session.types[tid].plant_id
        if self.template_only.get():
            self.description.configure(text='Используются условия расчётного шаблона. Это не подтверждение пригодности конкретного вида.');return
        self.description.configure(text=('Индивидуальный выбор карты не меняет каталог и посадки.' if override else
            'Из проектного типа: '+self.app.plant_names.get(pid, 'растение не задано')+
            '. Проверки, требующие растение, могут вернуть «Недостаточно данных».'))

    def accept(self):
        try:
            tid = self.type_ids[self.type_combo.current()]
            pid = self.plant_ids[self.plant_combo.current()] if self.override.get() else None
            if self.override.get() and pid is None:
                raise EditorError('Выберите конкретное растение либо снимите флажок индивидуального выбора.')
            options = MapOptions(tid, pid, self.root_protection.get(),
                                 self.step.get().replace(',', '.'), self.template_only.get()).checked(self.app.session.types)
        except (EditorError, ValueError) as exc:
            messagebox.showerror('Параметры карты', str(exc), parent=self)
            return
        self.destroy()
        self.controller.start(options)


class AdmissibilityController:
    def __init__(self, app):
        self.app = app
        self.options = None
        self.result = None
        self.owner = None
        self.running = False
        self.cancel_event = None
        self.progress_queue = queue.SimpleQueue()
        self._progress_after = None
        self.visible = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value='Карта не построена. Это проверка мест, а не расстановка.')
        self._layer_ids = [TOTAL]
        self.layer_id = TOTAL
        self._rectangles = []
        self.last_inspection = None
        self._inspection_is_current = False
        self._fresh = False
        self.options_window = None
        box = ttk.Frame(app.root, padding=(15, 3))
        box.pack(fill='x', before=app.body)
        box.columnconfigure(4, weight=1)
        self.build_button = ttk.Button(box, text='Карта допустимости…', command=self.choose)
        self.build_button.grid(row=0, column=0, sticky='w')
        self.inspect_button = ttk.Button(box, text='Проверить место', command=self.inspect_mode)
        self.inspect_button.grid(row=0, column=1, padx=5)
        self.save_button = ttk.Button(box, text='JSON карты…', command=self.save)
        self.save_button.grid(row=0, column=2, padx=(0, 5))
        ttk.Checkbutton(box, text='Показать', variable=self.visible, command=self.toggle).grid(row=0, column=3)
        self.layer_combo = ttk.Combobox(box, values=['Итог выбранных обязательных условий'], state='readonly', width=38)
        self.layer_combo.current(0)
        self.layer_combo.grid(row=0, column=4, sticky='ew', padx=8)
        self.layer_combo.bind('<<ComboboxSelected>>', self.change_layer)
        self.cancel_button = ttk.Button(box, text='Стоп', command=self.cancel, state='disabled')
        self.cancel_button.grid(row=0, column=5)
        ttk.Label(box, textvariable=self.status, wraplength=1050,
                  style='Muted.TLabel').grid(row=1, column=0, columnspan=6, sticky='w', pady=(4, 0))
        # Use the existing worker infrastructure. Stop remains enabled separately.
        app.toolbuttons.extend([self.build_button, self.inspect_button, self.save_button])
        app.canvas.overlay_renderer = self.draw
        app.canvas.on_inspect = self.inspect

    def state(self):
        if self.result is None or self.owner is not self.app.session:
            return 'NOT_BUILT'
        return 'CURRENT' if self.result.kernel.signature == map_fingerprint(self.app.session, self.options) else 'STALE'

    def choose(self):
        if not self.app.session or self.app.busy:
            return
        if self.options_window and self.options_window.winfo_exists():
            self.options_window.lift()
            return
        self.options_window = MapOptionsDialog(self)

    def start(self, options):
        app = self.app
        if not app.session or app.busy:
            return
        options = options.checked(app.session.types)
        if self._progress_after is not None:
            app.root.after_cancel(self._progress_after)
            self._progress_after = None
        app.canvas.cancel_drag()
        request = make_map_request(app.session, options)
        session = app.session
        self.options = options
        # Hide previous snapshot during computation, even when the new run fails.
        self.result = None
        self._rectangles = []
        self._fresh = False
        self.last_inspection = None
        self.running = True
        self.cancel_event = Event()
        self.cancel_button.configure(state='normal')
        self.status.set('Построение карты… Интерфейс отвечает; «Стоп» отменяет расчёт.')
        self.visible.set(True)
        self.progress_queue = queue.SimpleQueue()
        progress = self.progress_queue
        cancel = self.cancel_event
        app.canvas.request_render()

        def worker():
            try:
                return build_map(request, options, cancel=cancel,
                                 progress=lambda done, total: progress.put(('progress', done, total)))
            finally:
                progress.put(('finished', 0, 0))

        def finished(result):
            self.running = False
            self.cancel_button.configure(state='disabled')
            if result is None:
                self.status.set('Расчёт отменён. Частичная карта не используется.')
                return
            if app.session is not session or map_fingerprint(session, options) != request.fingerprint:
                self.status.set('Данные изменились во время расчёта — постройте карту заново.')
                return
            self.result, self.owner = result, session
            self._layer_ids = [TOTAL] + list(result.layers)
            names = ['Итог обязательных условий участка'] + [
                result.kernel.evaluator.by_id.get(r, {'label': r})['label'] for r in result.layers]
            self.layer_combo.configure(values=names)
            self.layer_combo.current(0)
            self.layer_id = TOTAL
            self._rectangles = list(result.runs())
            self.changed()
            self.inspect_mode()

        app.run_task(worker, finished, 'Вычисление карты допустимости…')
        self._poll_progress()

    def _poll_progress(self):
        self._progress_after = None
        try:
            while True:
                event, done, total = self.progress_queue.get_nowait()
                if event == 'progress' and self.running:
                    self.status.set(f'Карта: обработано {done:,} / {total:,} клеток. «Стоп» — отменить.')
        except queue.Empty:
            pass
        if not self.app.busy and self.running:
            # Failure is presented by EditorApp.run_task. Never retain old green.
            self.running = False
            self.cancel_button.configure(state='disabled')
            self.status.set('Карта не построена. Исправьте входные данные или увеличьте шаг.')
        if self.running:
            self._progress_after = self.app.root.after(80, self._poll_progress)

    def cancel(self):
        if self.cancel_event:
            self.cancel_event.set()
        self.status.set('Отмена после текущей операции…')

    def changed(self):
        if self.running:
            return
        if self.owner is not self.app.session:
            self.result = None
            self._rectangles = []
            self.last_inspection = None
            self.options = None
        state = self.state()
        self._fresh = state == 'CURRENT'
        if state == 'STALE':
            self.status.set('КАРТА УСТАРЕЛА И СКРЫТА — изменились типы, параметры, классификация или правила. Перестройте карту.')
            if self._inspection_is_current:
                self.app.set_info('Предыдущее объяснение карты устарело. Постройте карту заново.')
            self._inspection_is_current = False
        elif state == 'CURRENT':
            counts = self.result.counts(self.layer_id)
            plant = self.result.kernel.evaluator.context.effective_plant_id(self.result.kernel.prototype)
            template=self.app.session.types[self.options.type_id].template
            caption = self.app.plant_names.get(plant, 'расчётный шаблон (без вида)' if template else 'растение не задано')
            ignored = len(self.result.kernel.excluded)
            self.status.set(f'{self.options.type_id} · {caption} · шаг {self.options.step_m:g} м · '
                f"выполнено {counts.get('PASS',0)} / нарушено {counts.get('FAIL',0)} / нет данных {counts.get('UNKNOWN',0)}"
                + (f' · исключено проверок плана: {ignored}' if ignored else '')
                + (' · центры не попали в зону: уменьшите шаг' if self.result.sampled_count == 0 else '')
                + '\nЗелёный: выполнено; красный: запрет; жёлтый: нет данных; серый: не проверено; фиолетовый: ошибка; оранжевый: рекомендация. Только центры; TEST.')
        else:
            self.status.set('Карта не построена. Выберите тип и постройте слой; новые посадки не создаются.')
        self.app.canvas.request_render()

    def change_layer(self, event=None):
        i = self.layer_combo.current()
        if not self.result or i < 0 or i >= len(self._layer_ids):
            return
        self.layer_id = self._layer_ids[i]
        self._rectangles = list(self.result.runs(self.layer_id))
        self.changed()
        if self.last_inspection is not None and self._fresh:
            self._show_inspection(self.last_inspection)

    def toggle(self):
        if not self.visible.get() and self.app.canvas.mode == 'inspect':
            self.app.mode.set('select')
            self.app.mode_changed()
        self.app.canvas.request_render()

    def inspect_mode(self):
        if self.app.busy:
            return
        if self.state() != 'CURRENT':
            self.app._status('Сначала постройте актуальную карту допустимости.')
            return
        self.visible.set(True)
        self.app.mode.set('inspect')
        self.app.mode_changed()
        self.app._status('Щёлкните по карте: проверяется фактическая координата, не ближайшая клетка. ПКМ — сдвиг.')
        self.app.canvas.request_render()

    def inspect(self, x, y):
        if self.app.busy or self.state() != 'CURRENT' or not self.visible.get():
            self.app._status('Карта отсутствует или устарела — перестройте её.')
            return
        result = self.result
        session = self.app.session
        # Same prepared operators, exact clicked coordinates. Not a grid lookup.
        def done(row):
            if self.app.session is session and self.result is result and self.state() == 'CURRENT':
                self.last_inspection = row
                self._show_inspection(row)
                self.app.canvas.request_render()
        self.app.run_task(lambda: result.kernel.inspect(x, y), done, 'Проверка выбранной координаты…')

    def _show_inspection(self, row):
        app = self.app
        app.session.selected_id = None
        app.canvas.selected_feature = None
        app.on_select(None, None)
        app.editor.pack_forget()  # Expand explanation instead of disabled planting inputs.
        display = dict(row, id='map_probe', label='Точка карты')
        if self.layer_id != TOTAL:
            display['checks'] = [r for r in row['checks'] if r['rule_id'] == self.layer_id]
            display['status'] = display['checks'][0]['status'] if display['checks'] else 'NOT_CHECKED'
        app.selection_caption.configure(text=f"Точка карты ({row['x']:.3f}; {row['y']:.3f})\n"+
                                        STATUS_LABELS[display['status']])
        prefix = ('ВНЕ ОБЛАСТИ КАРТЫ. Ниже результаты выбранных правил в этой точке.\n\n'
                  if not row['inside_map_domain'] else '')
        if self.layer_id != TOTAL:
            prefix += 'ТОЛЬКО ОДНО ПРАВИЛО, не итог всех допусков.\n\n'
        display['checks'] = sorted(display['checks'], key=lambda r: {'FAIL': 0, 'ERROR': 1, 'UNKNOWN': 2}.get(r['status'], 3))
        app.set_info(prefix+'Фактическая координата щелчка, без привязки к сетке.\n\n'+details(display)+
                     '\nКарта не учитывает другие новые посадки.\n'+
                     '\n'.join('Исключено: '+r['label'] for r in row['excluded_rules']))
        self._inspection_is_current = True
        app._status("Проверена точка карты. Цвет — выбранный слой, посадки не изменены.")

    def draw(self, canvas):
        if not self.result or not self._fresh or not self.visible.get():
            return
        w, h = canvas.winfo_width(), canvas.winfo_height()
        for a, b, c, d, status in self._rectangles:
            x0, y1 = canvas.to_screen(a, b)
            x1, y0 = canvas.to_screen(c, d)
            if x1 < 0 or x0 > w or y1 < 0 or y0 > h:
                continue
            canvas.create_rectangle(max(-1, x0), max(-1, y0), min(w+1, x1), min(h+1, y1),
                outline='', fill=COLOURS[status], stipple='gray50',
                tags=('admissibility', 'map_status:'+status))
        if self.last_inspection and self._fresh:
            x, y = canvas.to_screen(self.last_inspection['x'], self.last_inspection['y'])
            canvas.create_oval(x-5, y-5, x+5, y+5, outline='#193F30', width=2, tags=('map_probe',))
            canvas.create_line(x-9, y, x+9, y, fill='#193F30', tags=('map_probe',))
            canvas.create_line(x, y-9, x, y+9, fill='#193F30', tags=('map_probe',))

    def save(self):
        if self.app.busy or self.state() != 'CURRENT':
            self.app._status('Для выгрузки нужна актуальная карта допустимости.')
            return
        path = filedialog.asksaveasfilename(parent=self.app.root, defaultextension='.json',
                    initialfile='admissibility_map.json', filetypes=[('Карта JSON', '*.json')])
        if path:
            save_report(self.result.to_dict(), path)
            self.app._status('Карта и её параметры сохранены в JSON; исходный DXF не изменялся.')

    def shutdown(self):
        if self.cancel_event:
            self.cancel_event.set()
        if self._progress_after is not None:
            self.app.root.after_cancel(self._progress_after)
            self._progress_after = None
