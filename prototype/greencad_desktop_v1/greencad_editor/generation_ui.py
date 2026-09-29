"""Additive template generation: form, cancelable worker, ghost preview, one apply.

Only this adapter knows Tk. A preview is never inserted into EditorSession until
an explicit Apply, and disappears as soon as its source state is stale.
"""
from __future__ import annotations

from copy import deepcopy
import json
import logging
import queue
from threading import Event
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .checks import fingerprint, make_request, save_report
from .checks_ui import details, _ScrollableTab
from .generation import (GenerationOptions, GenerationTarget, generate_plan, apply_generation,
                         zone_choices, MAX_NEW_PLACEMENTS, MAX_GRID_POINTS, REQUIRED_RULES,
                         LAYOUT_LABELS, MAX_SEED, MAX_TARGETS)
from .errors import EditorError
from .templates import profile_radius

LOG = logging.getLogger(__name__)


class GenerationDialog(tk.Toplevel):
    def __init__(self, controller):
        app = controller.app
        super().__init__(app.root)
        self.controller, self.app = controller, app
        self.title('Расставить типы — задание на генерацию')
        self.geometry('1000x760'); self.minsize(820, 520)
        self.transient(app.root); self.grab_set()
        self.owner = app.session
        self.zone_items = zone_choices(self.owner.drawing)
        self.order = list(self.owner.types)
        available = [tid for tid in self.order if self.owner.types[tid].template is not None]
        self.enabled = set(available[:4])
        self.caps = {tid: 20 for tid in self.order}
        self.current = None
        self._refreshing = False
        old = controller.options
        if old and old.zone_id in [z['id'] for z in self.zone_items]:
            known = [t.type_id for t in old.targets if t.type_id in self.owner.types]
            self.order = known + [k for k in self.order if k not in known]
            self.enabled = set(known)
            for t in old.targets:
                if t.type_id in self.caps: self.caps[t.type_id] = t.max_new
        outer = ttk.Frame(self, padding=12); outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1); outer.rowconfigure(0, weight=1)
        self.form_scroll = _ScrollableTab(outer, padding=4)
        self.form_scroll.grid(row=0, column=0, sticky='nsew')
        frame = self.form_scroll.content
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text='ТИПЫ ПО ОЧЕРЕДИ → ПРОВЕРЕННЫЙ ПРЕДВАРИТЕЛЬНЫЙ ПЛАН',
                  style='Header.TLabel').grid(row=0, column=0, sticky='w')
        header = ttk.Frame(frame); header.grid(row=1, column=0, sticky='ew', pady=8)
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text='Зелёная зона').grid(row=0, column=0, sticky='w')
        self.zone_combo = ttk.Combobox(header, state='readonly', values=[z['label']+' ['+z['id']+']' for z in self.zone_items])
        self.zone_combo.grid(row=0, column=1, sticky='ew', padx=10)
        if self.zone_items:
            ids = [z['id'] for z in self.zone_items]
            self.zone_combo.current(ids.index(old.zone_id) if old and old.zone_id in ids else 0)
        ttk.Label(header, text='Шаг поиска, м').grid(row=1, column=0, sticky='w', pady=6)
        self.step = tk.StringVar(value=str(old.step_m if old else 1.0))
        ttk.Entry(header, textvariable=self.step, width=10).grid(row=1, column=1, sticky='w', padx=10)
        ttk.Label(header, text=f'До {MAX_TARGETS} типов; до {MAX_GRID_POINTS:,} пробных точек; до {MAX_NEW_PLACEMENTS:,} новых посадок всего. '
            'Размер шага не ограничивает ручное перемещение.', wraplength=880,
            style='Muted.TLabel').grid(row=2, column=0, columnspan=2, sticky='w', pady=5)
        ttk.Label(header, text='Расположение').grid(row=3, column=0, sticky='w', pady=5)
        style_row = ttk.Frame(header); style_row.grid(row=3, column=1, sticky='ew', padx=10)
        self.layout_keys = list(LAYOUT_LABELS)
        self.layout_combo = ttk.Combobox(style_row, state='readonly', width=29, values=list(LAYOUT_LABELS.values()))
        self.layout_combo.pack(side='left')
        self.layout_combo.current(self.layout_keys.index(old.layout if old else 'natural'))
        ttk.Label(style_row, text='  Вариант:').pack(side='left', padx=(8, 4))
        self.seed = tk.StringVar(value=str(old.seed if old else 1))
        self.seed_entry = ttk.Entry(style_row, textvariable=self.seed, width=11); self.seed_entry.pack(side='left')
        self.next_button = ttk.Button(style_row, text='Другой вариант', command=self.next_variant)
        self.next_button.pack(side='left', padx=6)
        self.layout_combo.bind('<<ComboboxSelected>>', self.layout_changed)
        ttk.Label(header, text='Свободная: пробные точки по всему участку без ровных рядов. '
            'Одинаковый номер и те же исходные данные повторяют вариант. Все отступы проверяются; '
            'количество может оказаться меньше, чем при расстановке рядами.', wraplength=880,
            style='Muted.TLabel').grid(row=4, column=0, columnspan=2, sticky='w', pady=5)
        self.layout_changed()
        middle = ttk.Frame(frame); middle.grid(row=2, column=0, sticky='nsew')
        middle.columnconfigure(0, weight=1); middle.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(middle, columns=('on', 'type', 'radius', 'cap', 'plant'), show='headings', height=7)
        for key, title, width in [('on', 'Вкл.', 46), ('type', 'Порядок / тип', 320), ('radius', 'Резерв, м', 100),
                                  ('cap', 'Добавить не более', 140), ('plant', 'Наполнение', 200)]:
            self.tree.heading(key, text=title); self.tree.column(key, width=width, minwidth=40, stretch=key in ('type', 'plant'))
        self.tree.grid(row=0, column=0, sticky='nsew')
        ybar = ttk.Scrollbar(middle, command=self.tree.yview); ybar.grid(row=0, column=1, sticky='ns')
        xbar = ttk.Scrollbar(middle, orient='horizontal', command=self.tree.xview); xbar.grid(row=1, column=0, sticky='ew')
        self.tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.tree.bind('<Button-1>', self.toggle_click)
        self.tree.bind('<space>', self.toggle_key)
        self.tree.bind('<<TreeviewSelect>>', self.select_row)
        editor = ttk.Frame(frame); editor.grid(row=3, column=0, sticky='ew', pady=10)
        self.up_button = ttk.Button(editor, text='↑ Раньше', command=lambda: self.move(-1)); self.up_button.pack(side='left')
        self.down_button = ttk.Button(editor, text='↓ Позже', command=lambda: self.move(1)); self.down_button.pack(side='left', padx=6)
        ttk.Label(editor, text=f'Лимит типа (1–{MAX_NEW_PLACEMENTS}):').pack(side='left', padx=10)
        self.cap = tk.StringVar(value='20')
        self.cap_entry = ttk.Entry(editor, textvariable=self.cap, width=8); self.cap_entry.pack(side='left')
        self.cap_button = ttk.Button(editor, text='Записать лимит', command=self.store_cap); self.cap_button.pack(side='left', padx=6)
        ttk.Label(frame, text='Лимиты относятся только к НОВЫМ экземплярам. Ручные посадки не удаляются и не перемещаются. '
            'Большие типы поставьте выше, но порядок выбираете вы. Назначенные растения, если они есть, также проверяются.',
            style='Muted.TLabel', wraplength=880).grid(row=4, column=0, sticky='w', pady=(0, 5))
        ttk.Label(frame, text='Обязательный минимум генератора: зелёная зона, исключённые площади, межпосадочные расстояния. '
            'Недостающие флажки будут добавлены к заданию и сохранятся в проекте только после «Применить». '
            'Неизвестные обязательные данные не считаются разрешением. Пороги по-прежнему TEST.',
            wraplength=880).grid(row=5, column=0, sticky='w', pady=5)
        # Outside the scrolling form: actions stay visible at small windows / high DPI.
        footer = ttk.Frame(outer); footer.grid(row=1, column=0, sticky='ew', pady=(8, 0))
        self.build_button = ttk.Button(footer, text='Построить вариант', style='Accent.TButton', command=self.accept)
        self.build_button.pack(side='right')
        self.cancel_button = ttk.Button(footer, text='Отмена', command=self.destroy); self.cancel_button.pack(side='right', padx=8)
        self.bind('<Escape>', lambda e: self.destroy())
        self.bind('<Control-Return>', lambda e: self.accept())
        self.populate()
        if self.order: self.tree.selection_set(self.order[0]); self.select_row()

    def layout_changed(self, event=None):
        active = self.layout_keys[self.layout_combo.current()] == 'natural'
        self.seed_entry.configure(state='normal' if active else 'disabled')
        self.next_button.configure(state='normal' if active else 'disabled')

    def next_variant(self):
        try:
            seed = int(self.seed.get().strip())
            if not 0 <= seed <= MAX_SEED:
                raise ValueError()
        except ValueError:
            messagebox.showerror('Номер варианта', f'Введите целое от 0 до {MAX_SEED}.', parent=self)
            return
        self.seed.set(str((seed + 1) % (MAX_SEED + 1)))

    def _flush_cap(self):
        if self.current is not None:
            text = self.cap.get().strip()
            try: value = int(text)
            except (ValueError, TypeError): raise EditorError('Лимит должен быть целым числом.')
            if not 1 <= value <= MAX_NEW_PLACEMENTS: raise EditorError(f'Лимит от 1 до {MAX_NEW_PLACEMENTS}.')
            self.caps[self.current] = value

    def store_cap(self):
        try: self._flush_cap(); self.populate()
        except EditorError as exc: messagebox.showerror('Лимит типа', str(exc), parent=self)

    def populate(self):
        selected = self.tree.selection()
        self._refreshing = True
        self.tree.delete(*self.tree.get_children())
        for n, tid in enumerate(self.order, 1):
            t = self.owner.types[tid]
            radius = profile_radius(t)
            self.tree.insert('', 'end', iid=tid, values=('☑' if tid in self.enabled else '☐',
                f'{n}. {tid} — {t.name}', 'не задан' if radius is None else f'{radius:g}', self.caps[tid],
                ('Нет профиля' if t.template is None else ('Назначен вид' if t.plant_id else 'Анонимный шаблон'))))
        if selected and self.tree.exists(selected[0]): self.tree.selection_set(selected[0])
        self._refreshing = False

    def select_row(self, event=None):
        if self._refreshing: return
        selection = self.tree.selection()
        if not selection or selection[0] == self.current: return
        try: self._flush_cap()
        except EditorError as exc:
            self.tree.selection_set(self.current)
            messagebox.showerror('Лимит типа', str(exc), parent=self); return
        self.current = selection[0]; self.cap.set(str(self.caps[self.current]))
        self.populate()

    def toggle(self, tid):
        if self.owner.types[tid].template is None:
            messagebox.showinfo('Расчётный тип', 'Сначала включите расчётный шаблон в карточке этого типа.', parent=self); return
        if tid in self.enabled: self.enabled.remove(tid)
        else: self.enabled.add(tid)
        self.populate()

    def toggle_click(self, event):
        tid = self.tree.identify_row(event.y)
        if tid and self.tree.identify_column(event.x) == '#1':
            self.toggle(tid); return 'break'

    def toggle_key(self, event=None):
        if self.tree.selection(): self.toggle(self.tree.selection()[0])
        return 'break'

    def move(self, direction):
        if not self.tree.selection(): return
        try: self._flush_cap()
        except EditorError as exc: messagebox.showerror('Лимит типа', str(exc), parent=self); return
        tid = self.tree.selection()[0]; i = self.order.index(tid); j = i + direction
        if 0 <= j < len(self.order):
            self.order[i], self.order[j] = self.order[j], self.order[i]
            self.populate(); self.tree.see(tid)

    def accept(self):
        try:
            if self.app.session is not self.owner: raise EditorError('Открыт другой проект. Повторите задание.')
            self._flush_cap()
            if self.zone_combo.current() < 0: raise EditorError('В исходном DXF нет размеченной зелёной зоны.')
            options = GenerationOptions(self.zone_items[self.zone_combo.current()]['id'],
                tuple(GenerationTarget(t, self.caps[t]) for t in self.order if t in self.enabled),
                self.step.get().replace(',', '.'),
                self.layout_keys[self.layout_combo.current()],
                int(self.seed.get().strip()) if self.layout_keys[self.layout_combo.current()] == 'natural' else 1
            ).checked(self.owner.types, len(self.owner.placements))
        except (EditorError, ValueError) as exc:
            messagebox.showerror('Задание расстановки', str(exc), parent=self); return
        self.destroy(); self.controller.start(options)


class GenerationReview(tk.Toplevel):
    def __init__(self, controller):
        super().__init__(controller.app.root)
        self.controller = controller
        self.title('Предварительная расстановка — результаты и причины')
        self.geometry('980x700'); self.minsize(730, 500)
        frame = ttk.Frame(self, padding=14); frame.pack(fill='both', expand=True)
        frame.columnconfigure(0, weight=1); frame.rowconfigure(1, weight=1)
        self.caption = ttk.Label(frame, textvariable=controller.status, wraplength=900, style='Header.TLabel')
        self.caption.grid(row=0, column=0, sticky='ew', pady=6)
        tabs = ttk.Notebook(frame); tabs.grid(row=1, column=0, sticky='nsew')
        self.pages = {}
        for key, label in [('summary', 'Итог'), ('checks', 'Проверка плана'), ('rejected', 'Причины пропусков')]:
            box = ttk.Frame(tabs); tabs.add(box, text=label)
            text = tk.Text(box, wrap='word', relief='flat', padx=10, pady=10)
            bar = ttk.Scrollbar(box, command=text.yview); bar.pack(side='right', fill='y')
            text.configure(yscrollcommand=bar.set); text.pack(fill='both', expand=True); self.pages[key] = text
        footer = ttk.Frame(frame); footer.grid(row=2, column=0, sticky='ew', pady=8)
        ttk.Button(footer, text='JSON результата…', command=controller.save).pack(side='left')
        self.apply_button = ttk.Button(footer, text='Применить вариант', style='Accent.TButton', command=controller.apply)
        self.apply_button.pack(side='right')
        ttk.Button(footer, text='Закрыть', command=self.destroy).pack(side='right', padx=8)
        self.bind('<Escape>', lambda e: self.destroy()); self.refresh()

    def refresh(self):
        result = self.controller.result
        self.apply_button.configure(state='normal' if result and result.can_apply and self.controller.state() == 'CURRENT' else 'disabled')
        summary, checks, rejected = 'Нет результата.', '', ''
        if result:
            r = result.report
            summary = (f"Состояние: {r['status']}\n{r.get('message','')}\n\n"
                f"Ручных посадок: {r['manual_count']}\nНовых: {r['new_count']}\n"
                f"Зона: {result.options.zone_id}\nШаг: {result.options.step_m:g} м\n"
                f"Расположение: {LAYOUT_LABELS[result.options.layout]}\n"
                f"Номер варианта: {result.options.seed if result.options.layout == 'natural' else 'не используется'}\n"
                f"Обязательные проверки: {r.get('mandatory_status',r.get('baseline_status'))}\n"
                f"Дополнительно включены: {', '.join(r['added_rules']) or 'нет'}\n\n")
            for s in r['statistics']:
                summary += f"{s['type_id']} — {s['name']}: {s['placed']} / {s['max_new']}; просмотрено {s['examined']}; {s['stop_reason']}\n"
            order_note = ('Строки по Y, внутри строки по X.' if result.options.layout == 'grid' else
                          'Случайная точка внутри каждой поисковой клетки; перемешанный порядок. '
                          'Проверка выполняется после выбора координат; после проверки точки не сдвигаются.')
            summary += '\n' + r['scope'] + '\n\n' + order_note + '\nКвота — верхний предел новых экземпляров.'
            rows = result.validation['placements']
            checks = '\n\n'.join(details(row) for row in rows[:100])
            if len(rows) > 100: checks += '\n\nПоказаны первые 100 посадок; полный отчёт — JSON.'
            records = r['rejections']
            rejected = 'Показаны первые 100 рассмотренных отказов. Все рассмотренные отказы сохранены в JSON.\nНе просмотренные после достижения квоты точки отказами не считаются.\n\n'
            for row in records[:100]:
                rejected += f"{row['type_id']} ({row['x']:g}, {row['y']:g}): {row['status']}\n"
                rejected += '\n'.join(f"  {c['rule_id']}: {c['status']}; {c['reason']}" for c in row['checks']) + '\n\n'
        for key, value in [('summary', summary), ('checks', checks), ('rejected', rejected)]:
            text = self.pages[key]; text.configure(state='normal'); text.delete('1.0', 'end'); text.insert('1.0', value); text.configure(state='disabled')


class GenerationController:
    def __init__(self, app):
        self.app = app; self.result = None; self.owner = None; self.options = None
        self.running = False; self.applied = False; self._fresh = False
        self.cancel_event = None; self.progress_queue = queue.SimpleQueue(); self._after = None
        self.options_window = None; self.review_window = None
        self.status = tk.StringVar(value='Авторасстановка: сначала вариант, затем явное применение. Ручные посадки сохраняются.')
        box = ttk.Frame(app.root, padding=(15, 4)); box.pack(fill='x', before=app.body)
        box.columnconfigure(4, weight=1)
        self.build_button = ttk.Button(box, text='Расставить типы…', command=self.choose)
        self.build_button.grid(row=0, column=0)
        self.apply_button = ttk.Button(box, text='Применить вариант', style='Accent.TButton', command=self.apply, state='disabled')
        self.apply_button.grid(row=0, column=1, padx=6)
        self.review_button = ttk.Button(box, text='Результат…', command=self.show, state='disabled'); self.review_button.grid(row=0, column=2)
        self.discard_button = ttk.Button(box, text='Убрать вариант', command=self.discard, state='disabled'); self.discard_button.grid(row=0, column=3, padx=6)
        ttk.Label(box, text='Пунктир — ещё не принятые посадки', style='Muted.TLabel').grid(row=0, column=4, sticky='w', padx=5)
        self.stop_button = ttk.Button(box, text='Стоп', command=self.cancel, state='disabled'); self.stop_button.grid(row=0, column=5)
        ttk.Label(box, textvariable=self.status, wraplength=1060, style='Muted.TLabel').grid(row=1, column=0, columnspan=6, sticky='w', pady=(3, 0))
        app.toolbuttons.extend([self.build_button, self.apply_button, self.review_button, self.discard_button])
        app.canvas.preview_renderer = self.draw

    def state(self):
        if self.result is None or self.owner is not self.app.session: return 'NOT_BUILT'
        current = fingerprint(self.app.session)
        if self.applied: return 'APPLIED' if current == self.result.validation['fingerprint'] else 'HISTORY'
        return 'CURRENT' if current == self.result.base_fingerprint else 'STALE'

    def choose(self):
        if not self.app.session or self.app.busy: return
        if self.options_window and self.options_window.winfo_exists(): self.options_window.lift(); return
        self.options_window = GenerationDialog(self)

    def start(self, options):
        app = self.app
        if not app.session or app.busy: return
        app.canvas.cancel_drag()
        try:
            options = options.checked(app.session.types, len(app.session.placements))
            request = make_request(app.session)
        except EditorError as exc: messagebox.showerror('Расстановка', str(exc), parent=app.root); return
        self.options = options; self.result = None; self.applied = False; self._fresh = False
        self.running = True; session = app.session; self.cancel_event = Event()
        self.progress_queue = queue.SimpleQueue(); progress, cancel = self.progress_queue, self.cancel_event
        self.status.set('Генерация: подготовка и проверка текущего плана…')
        self.stop_button.configure(state='normal'); app.canvas.request_render()
        def worker():
            try:
                result = generate_plan(request, options, cancel=cancel,
                    progress=lambda stage, n, total: progress.put((stage, n, total)))
                return result, None
            except Exception as exc:
                LOG.exception('Генерация не выполнена')
                return None, str(exc)
        def finished(outcome):
            self.running = False; self.stop_button.configure(state='disabled')
            if self._after is not None:
                app.root.after_cancel(self._after); self._after = None
            result, error = outcome
            if error:
                self.status.set('Расстановка не выполнена; проект не изменён.')
                messagebox.showerror('Расстановка', error, parent=app.root)
            elif result is None or cancel.is_set():
                self.status.set('Расстановка отменена. Частичный вариант не применяется.')
            elif app.session is not session or fingerprint(session) != request.fingerprint:
                self.status.set('Проект изменился во время расчёта — повторите расстановку.')
            else:
                self.result, self.owner = result, session
                self.changed()
                if result.report['status'] in ('BASELINE_BLOCKED', 'VALIDATION_BLOCKED', 'EMPTY'): self.show()
            app._status("Расстановка завершена; подробности — в панели генерации и «Результат…».")
            self._buttons(); app.canvas.request_render()
        app.run_task(worker, finished, 'Построение предварительной расстановки…')
        self._poll()

    def _poll(self):
        self._after = None
        latest = None
        while True:
            try: latest = self.progress_queue.get_nowait()
            except queue.Empty: break
        if latest and self.running:
            stage, n, total = latest
            self.status.set(f'{stage} — {n:,} / {total:,}. Проект пока не изменён.')
        if self.running: self._after = self.app.root.after(100, self._poll)

    def _buttons(self):
        ready = not self.app.busy and self.result is not None and self.owner is self.app.session
        self.apply_button.configure(state='normal' if ready and not self.applied and self.result.can_apply and self.state() == 'CURRENT' else 'disabled')
        self.review_button.configure(state='normal' if ready else 'disabled')
        self.discard_button.configure(state='normal' if ready else 'disabled')

    def changed(self):
        if self.running: return
        state = self.state(); self._fresh = state == 'CURRENT'
        if state == 'NOT_BUILT':
            self.result = None
        elif state == 'STALE': self.status.set('ВАРИАНТ УСТАРЕЛ: проект изменён. Пунктир скрыт; пересчитайте расстановку.')
        elif state == 'HISTORY': self.status.set('Проект изменён после применения; сохранён исторический отчёт расстановки.')
        elif state == 'APPLIED': self.status.set(f'Добавлено {len(self.result.placements)} посадок. Ctrl+Z отменяет всю группу; Ctrl+S сохраняет проект.')
        else:
            r = self.result.report
            totals = ' · '.join(f"{s['type_id']}: {s['placed']}/{s['max_new']}" for s in r['statistics'])
            variant = (f'Свободная · вариант {self.result.options.seed}. ' if self.result.options.layout == 'natural' else 'Рядами. ')
            self.status.set(f"ПРЕДВАРИТЕЛЬНО: {variant}{totals or r.get('message', '')}. " +
                            ('Нажмите «Применить вариант». Ручные посадки не изменены.' if self.result.can_apply else 'Откройте «Результат…».'))
        self._buttons(); self.app.canvas.request_render()
        if self.review_window and self.review_window.winfo_exists(): self.review_window.refresh()

    def apply(self):
        if self.app.busy or not self.result or self.applied: return
        try: count = apply_generation(self.app.session, self.result)
        except EditorError as exc: messagebox.showerror('Применение варианта', str(exc), parent=self.app.root); self.changed(); return
        self.applied = True; self._fresh = False
        self.app.on_change(); self.app.on_select(self.app.session.selected_id, None)
        self.status.set(f'Добавлено {count} посадок одним действием. Ctrl+Z — отменить, Ctrl+S — сохранить .gcp.')
        self._buttons()

    def discard(self):
        if self.app.busy: return
        self.result = None; self.owner = None; self._fresh = False; self.applied = False
        self.status.set('Предварительный вариант убран. Принятые и ручные посадки не изменены.')
        self.changed()

    def show(self):
        if not self.result or self.owner is not self.app.session: return
        if self.review_window and self.review_window.winfo_exists(): self.review_window.refresh(); self.review_window.lift(); return
        self.review_window = GenerationReview(self)

    def save(self):
        if not self.result: return
        name = filedialog.asksaveasfilename(parent=self.review_window or self.app.root,
            defaultextension='.json', initialfile='generation_report.json', filetypes=[('JSON отчёта', '*.json')])
        if name:
            try: save_report({'state': self.state(), 'result': self.result.export()}, name)
            except (EditorError, OSError) as exc: messagebox.showerror('JSON расстановки', str(exc), parent=self.app.root)

    def cancel(self):
        if self.cancel_event: self.cancel_event.set()
        self.status.set('Остановка расстановки… Частичный результат не будет добавлен.')

    def draw(self, canvas):
        if not self._fresh or self.result is None or self.applied: return
        w, h = canvas.winfo_width(), canvas.winfo_height()
        for p in self.result.placements:
            t = self.app.session.types[p.type_id]
            x, y = canvas.to_screen(p.x, p.y)
            r = max(5, (profile_radius(t) or t.radius_m) * canvas.scale_px)
            if x+r < 0 or y+r < 0 or x-r > w or y-r > h: continue
            tags = ('generation_preview', 'generation_type:'+t.id)
            canvas.create_oval(x-r, y-r, x+r, y+r, outline=t.color, width=2, dash=(4, 3), tags=tags)
            canvas.create_line(x-3, y, x+3, y, fill=t.color, tags=tags)
            canvas.create_line(x, y-3, x, y+3, fill=t.color, tags=tags)
            if canvas.show_labels and len(self.result.placements) <= 300:
                canvas.create_text(x+r+3, y, text=t.id, fill=t.color, anchor='w', font=('Segoe UI', 9), tags=tags)

    def shutdown(self):
        if self.cancel_event: self.cancel_event.set()
        if self._after is not None:
            try: self.app.root.after_cancel(self._after)
            except tk.TclError: pass
            self._after = None
