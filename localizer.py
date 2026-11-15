#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sys, csv, json, struct, re, threading, queue, traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, 'data')

def resource_path(name):
    """Путь к ресурсу: рядом с .py."""
    base = getattr(sys, '_MEIPASS', APP_DIR)
    p = os.path.join(base, 'data', name)
    return p if os.path.isfile(p) else os.path.join(DATA_DIR, name)

PRINTABLE = re.compile(r'^[\x20-\x7e\u00a0-\uffff\n\r\t\\]*$')
ALLOWED_SCRIPTS = {'Text', 'DialogueManager', 'DialogueCharacterDatabase'}

FONT_SLOTS = [
    ('sharedassets2.assets', 34, 'Notepen (рукописный)'),
    ('sharedassets1.assets', 5,  'Retro Gaming (пиксельный)'),
]

def read_string_at(buf, off):
    if off + 4 > len(buf):
        return None
    n, = struct.unpack_from('<i', buf, off)
    if n <= 0 or off + 4 + n > len(buf) or n > 65536:
        return None
    try:
        text = buf[off+4:off+4+n].decode('utf-8')
    except UnicodeDecodeError:
        return None
    if not PRINTABLE.match(text) or text.strip() == '':
        return None
    return text, (off + 4 + n + 3) & ~3

def walk_strings(buf, start):
    found, off = [], start
    while off + 4 <= len(buf):
        s = read_string_at(buf, off)
        if s is None:
            off += 4
            continue
        found.append((off, s[0]))
        off = s[1]
    return found

def list_data_files(data_dir):
    names = []
    for f in sorted(os.listdir(data_dir)):
        if f.endswith(('.resS', '.resource')):
            continue
        if f.endswith('.assets') or re.fullmatch(r'level\d+', f):
            names.append(f)
    return names

def scan_data(data_dir, log):
    """Возвращает inventory: [{file, pathID, offset, text, script}]."""
    import UnityPy
    files = list_data_files(data_dir)
    log(f'Сканирование {len(files)} файлов...')
    script_map = {}
    envs = {}
    for f in files:
        envs[f] = UnityPy.load(os.path.join(data_dir, f))
    for f, env in envs.items():
        for obj in env.objects:
            if obj.type.name == 'MonoScript':
                try:
                    script_map[(f, obj.path_id)] = obj.read().m_Name
                except Exception:
                    pass
    inventory = []
    for f, env in envs.items():
        try:
            externals = [os.path.basename(e.path) for e in env.file.externals]
        except Exception:
            externals = []
        count_mo = 0
        for obj in env.objects:
            if obj.type.name != 'MonoBehaviour':
                continue
            count_mo += 1
            buf = obj.get_raw_data()
            if len(buf) < 32:
                continue
            fid, pid = struct.unpack_from('<iq', buf, 16)
            script = None
            if fid == 0:
                script = script_map.get((f, pid))
            elif 1 <= fid <= len(externals):
                script = script_map.get((externals[fid-1], pid))
            if script not in ALLOWED_SCRIPTS:
                continue
            n, = struct.unpack_from('<i', buf, 28)
            name_end = (32 + max(n, 0) + 3) & ~3
            for off, text in walk_strings(buf, name_end):
                inventory.append({'file': f, 'pathID': obj.path_id,
                                  'offset': off, 'text': text, 'script': script})
        log(f'  {f}: MonoBehaviour={count_mo}, строк найдено: '
            f'{sum(1 for i in inventory if i["file"] == f)}')
    return inventory

def font_display_name(ttf_bytes):
    try:
        from fontTools.ttLib import TTFont
        import io
        tt = TTFont(io.BytesIO(ttf_bytes), fontNumber=0)
        return tt['name'].getDebugName(4) or '?'
    except Exception:
        return '?'

def detect_patched(data_dir, log):
    """True, если в sharedassets2 вместо Notepen уже другой шрифт."""
    try:
        import UnityPy
        env = UnityPy.load(os.path.join(data_dir, 'sharedassets2.assets'))
        for obj in env.objects:
            if obj.type.name == 'Font' and obj.path_id == 34:
                fd = obj.read().m_FontData
                fd = bytes(fd) if isinstance(fd, list) else fd
                name = font_display_name(fd)
                if name and 'Notepen' not in name:
                    log(f'ВНИМАНИЕ: в sharedassets2 уже стоит шрифт «{name}» — '
                        f'похоже, файлы уже патченные. Скан/патч по ним даст нулевой результат. '
                        f'Укажи папку _Data ОРИГИНАЛЬНОЙ игры.')
                    return True
    except Exception:
        pass
    return False

def splice_string(buf: bytearray, off: int, old: str, new: str):
    n, = struct.unpack_from('<i', buf, off)
    if buf[off+4:off+4+n].decode('utf-8') != old:
        raise RuntimeError(f'смещение разъехалось: ожидалось {old!r}')
    m = len(new.encode('utf-8'))
    old_end = (off + 4 + n + 3) & ~3
    pad = (-(4 + m)) % 4
    buf[off:old_end] = struct.pack('<i', m) + new.encode('utf-8') + b'\x00' * pad

def replace_font(env, path_id, ttf_path):
    with open(ttf_path, 'rb') as f:
        new_ttf = f.read()
    for obj in env.objects:
        if obj.type.name != 'Font' or obj.path_id != path_id:
            continue
        buf = bytearray(obj.get_raw_data())
        magic = b'\x00\x01\x00\x00'
        pos = -1
        for start in range(4, len(buf) - 8):
            pos = buf.find(magic, start)
            if pos == -1:
                break
            n, = struct.unpack_from('<i', buf, pos - 4)
            if 1000 <= n <= 50_000_000 and pos - 4 + 4 + n <= len(buf):
                break
        if pos == -1:
            raise RuntimeError('TTF-массив не найден в Font-объекте')
        arr_start = pos - 4
        old_len = n
        old_end = (arr_start + 4 + old_len + 3) & ~3
        pad = (-(4 + len(new_ttf))) % 4
        buf[arr_start:old_end] = struct.pack('<i', len(new_ttf)) + new_ttf + b'\x00' * pad
        obj.set_raw_data(bytes(buf))
        return True
    return False

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('The No Jumpscare Show — инструмент локализации')
        self.geometry('1150x760')
        self.minsize(950, 620)

        self.data_dir = tk.StringVar()
        self.out_dir = tk.StringVar()
        self.font_paths = {label: tk.StringVar() for _, _, label in FONT_SLOTS}
        self.inventory = []
        self.rows = []
        self.translations = {}
        self._row_map = {}
        self._pending_build = None
        self.filter_var = tk.StringVar()
        self.q = queue.Queue()
        self.busy = False

        self._build_ui()
        self.after(100, self._poll_queue)

        self._load_default_table()
        neucha = resource_path('Neucha.ttf')
        if os.path.isfile(neucha):
            self.font_paths['Notepen (рукописный)'].set(neucha)

    def _build_ui(self):
        pad = {'padx': 6, 'pady': 4}

        top = ttk.LabelFrame(self, text='1. Папка игры (оригинальная *_Data)')
        top.pack(fill='x', **pad)
        ttk.Entry(top, textvariable=self.data_dir).pack(side='left', fill='x', expand=True, padx=6, pady=6)
        ttk.Button(top, text='Обзор...', command=self.browse_data).pack(side='left', padx=4)
        ttk.Button(top, text='Сканировать', command=self.scan).pack(side='left', padx=(4, 8))

        mid = ttk.LabelFrame(self, text='2. Таблица перевода (двойной клик по «Перевод» — редактировать)')
        mid.pack(fill='both', expand=True, **pad)

        tb = ttk.Frame(mid); tb.pack(fill='x', padx=6, pady=4)
        ttk.Button(tb, text='CSV открыть', command=self.open_csv).pack(side='left', padx=2)
        ttk.Button(tb, text='CSV сохранить', command=self.save_csv).pack(side='left', padx=2)
        ttk.Button(tb, text='JSON открыть', command=self.open_json).pack(side='left', padx=2)
        ttk.Button(tb, text='JSON сохранить', command=self.save_json).pack(side='left', padx=2)
        ttk.Button(tb, text='Новая таблица из скана', command=self.new_table).pack(side='left', padx=2)
        self.stats_var = tk.StringVar(value='таблица пуста — отсканируй игру и переводи, или открой готовый файл')
        ttk.Label(tb, textvariable=self.stats_var).pack(side='right', padx=6)

        ff = ttk.Frame(mid); ff.pack(fill='x', padx=6, pady=(0, 4))
        ttk.Label(ff, text='Фильтр:').pack(side='left')
        e = ttk.Entry(ff, textvariable=self.filter_var, width=40)
        e.pack(side='left', padx=4)
        e.bind('<KeyRelease>', lambda _e: self.refresh_table())
        ttk.Button(ff, text='Сброс', command=lambda: self.filter_var.set('')).pack(side='left')

        cols = ('original', 'translation', 'script', 'file', 'count')
        self.tree = ttk.Treeview(mid, columns=cols, show='headings')
        self.tree.heading('original', text='Оригинал')
        self.tree.heading('translation', text='Перевод')
        self.tree.heading('script', text='Скрипт')
        self.tree.heading('file', text='Файл (первое вхождение)')
        self.tree.heading('count', text='Вхожд.')
        self.tree.column('original', width=420, anchor='w')
        self.tree.column('translation', width=420, anchor='w')
        self.tree.column('script', width=110, anchor='w')
        self.tree.column('file', width=110, anchor='w')
        self.tree.column('count', width=55, anchor='center')
        vsb = ttk.Scrollbar(mid, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side='left', fill='both', expand=True, padx=(6, 0), pady=(0, 6))
        vsb.pack(side='left', fill='y', pady=(0, 6))
        self.tree.bind('<Double-1>', self.edit_cell)

        fonts = ttk.LabelFrame(self, text='3. Шрифты (TTF с кириллицей; пусто = оставить родной)')
        fonts.pack(fill='x', **pad)
        for _, _, label in FONT_SLOTS:
            row = ttk.Frame(fonts); row.pack(fill='x', padx=6, pady=2)
            ttk.Label(row, text=label + ':', width=26).pack(side='left')
            ttk.Entry(row, textvariable=self.font_paths[label]).pack(side='left', fill='x', expand=True, padx=4)
            ttk.Button(row, text='Обзор...', command=lambda l=label: self.pick_font(l)).pack(side='left')
            ttk.Button(row, text='Очистить', command=lambda l=label: self.font_paths[l].set('')).pack(side='left', padx=(2, 6))

        out = ttk.LabelFrame(self, text='4. Сборка (папка вывода; сама игра не изменяется)')
        out.pack(fill='x', **pad)
        ttk.Entry(out, textvariable=self.out_dir).pack(side='left', fill='x', expand=True, padx=6, pady=6)
        ttk.Button(out, text='Обзор...', command=self.browse_out).pack(side='left', padx=4)
        self.build_btn = ttk.Button(out, text='СОБРАТЬ патченные файлы', command=self.build)
        self.build_btn.pack(side='left', padx=(4, 8))

        self.log_box = scrolledtext.ScrolledText(self, height=8, state='disabled')
        self.log_box.pack(fill='both', expand=False, padx=6, pady=(0, 6))

    # ---------- helpers ----------
    def log(self, msg):
        self.q.put(('log', msg))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == 'log':
                    self.log_box.configure(state='normal')
                    self.log_box.insert('end', payload + '\n')
                    self.log_box.see('end')
                    self.log_box.configure(state='disabled')
                elif kind == 'scan_done':
                    self._on_scan_done(payload)
                elif kind == 'build_done':
                    self._on_build_done(payload)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _set_busy(self, busy, note=''):
        self.busy = busy
        state = 'disabled' if busy else 'normal'
        for w in self.tree.winfo_children():
            pass
        self.build_btn.configure(state=state)
        if note:
            self.title(note if busy else 'The No Jumpscare Show — инструмент локализации')

    def run_bg(self, fn, done):
        if self.busy:
            messagebox.showinfo('Занят', 'Дождитесь завершения текущей операции.')
            return
        def worker():
            try:
                result = fn()
                self.q.put((done[0], result))
            except Exception:
                err = traceback.format_exc()
                self.q.put(('log', 'ОШИБКА:\n' + err))
                self.q.put((done[0], None))
        self._set_busy(True)
        threading.Thread(target=worker, daemon=True).start()

    # ---------- действия ----------
    def browse_data(self):
        d = filedialog.askdirectory(title='Выбери папку *_Data игры (например "The No Jumpscare Show_Data")')
        if d:
            self.data_dir.set(d)

    def browse_out(self):
        d = filedialog.askdirectory(title='Куда положить патченные файлы')
        if d:
            self.out_dir.set(d)

    def pick_font(self, label):
        p = filedialog.askopenfilename(title=f'TTF для {label}',
                                       filetypes=[('Шрифты', '*.ttf *.otf'), ('Все файлы', '*.*')])
        if p:
            self.font_paths[label].set(p)

    def scan(self):
        d = self.data_dir.get().strip()
        if not os.path.isdir(d):
            messagebox.showerror('Ошибка', 'Укажи папку *_Data игры.')
            return
        if not any(os.path.isfile(os.path.join(d, f)) for f in ('sharedassets2.assets', 'globalgamemanagers.assets')):
            messagebox.showerror('Ошибка', 'Это не похоже на папку The No Jumpscare Show_Data.')
            return
        self.log_box.configure(state='normal'); self.log_box.delete('1.0', 'end'); self.log_box.configure(state='disabled')
        def job():
            inv = scan_data(d, self.log)
            detect_patched(d, self.log)
            return inv
        self.run_bg(job, ('scan_done',))

    def _on_scan_done(self, inv):
        if inv is None:
            self._set_busy(False)
            return
        self.inventory = inv
        uniq = {}
        for it in inv:
            u = uniq.setdefault(it['text'], {'text': it['text'], 'script': it['script'],
                                             'file': it['file'], 'count': 0})
            u['count'] += 1
        self.rows = sorted(uniq.values(), key=lambda r: (r['script'], r['text'].lower()))
        if not self.translations:
            self.translations = {r['text']: '' for r in self.rows}
        self.log(f'Готово: {len(inv)} вхождений, {len(self.rows)} уникальных строк.')
        self.refresh_table()
        self._set_busy(False)
        if getattr(self, '_pending_build', None):
            d, o = self._pending_build
            self._pending_build = None
            self.log('Продолжаю сборку...')
            self.run_bg(lambda: self._do_build(d, o), ('build_done',))

    def refresh_table(self):
        self.tree.delete(*self.tree.get_children())
        self._row_map = {}
        flt = self.filter_var.get().strip().lower()
        for i, r in enumerate(self.rows):
            tr = self.translations.get(r['text'], '')
            if flt and flt not in r['text'].lower() and flt not in tr.lower():
                continue
            tag = ('done',) if tr.strip() else ('todo',)
            self._row_map[str(i)] = r['text']
            self.tree.insert('', 'end', iid=str(i), values=(
                r['text'].replace('\n', '\\n'), tr.replace('\n', '\\n'),
                r['script'], r['file'], r['count']), tags=tag)
        self.tree.tag_configure('done', background='#e7f6e7')
        self.tree.tag_configure('todo', background='#fdeaea')
        total = len(self.rows)
        done = sum(1 for r in self.rows if self.translations.get(r['text'], '').strip())
        self.stats_var.set(f'Переведено: {done}/{total} ({100*done//total if total else 0}%)')

    def edit_cell(self, event):
        if self.tree.identify_region(event.x, event.y) != 'cell':
            return
        col = self.tree.identify_column(event.x)
        if col != '#2':
            return
        row_id = self.tree.focus()
        original = self._row_map.get(row_id)
        if original is None:
            return
        x, y, w, h = self.tree.bbox(row_id, col)
        cur = self.translations.get(original, '')
        ed = tk.Entry(self.tree, justify='left')
        ed.place(x=x, y=y, width=max(w, 420), height=h)
        ed.insert(0, cur)
        ed.focus_set()

        def commit(_e=None):
            val = ed.get().replace('\\n', '\n')
            self.translations[original] = val
            ed.destroy()
            self.refresh_table()

        def cancel(_e=None):
            ed.destroy()

        ed.bind('<Return>', commit)
        ed.bind('<Escape>', cancel)
        ed.bind('<FocusOut>', commit)

    def _table_path(self):
        return os.path.join(DATA_DIR, 'ru_default.json')

    def _load_default_table(self):
        p = resource_path('ru_default.json')
        if os.path.isfile(p):
            try:
                with open(p, encoding='utf-8') as f:
                    self.translations = json.load(f)
                self.log(f'Загружен встроенный русский перевод: {len(self.translations)} строк ({p})')
            except Exception as e:
                self.log(f'Не удалось загрузить ru_default.json: {e}')

    def new_table(self):
        if not self.rows:
            messagebox.showinfo('Сначала скан', 'Отсканируй папку игры, чтобы получить строки.')
            return
        if not messagebox.askyesno('Новая таблица', 'Заменить текущую таблицу пустым шаблоном из скана?'):
            return
        self.translations = {r['text']: '' for r in self.rows}
        self.refresh_table()
        self.log(f'Создан шаблон: {len(self.rows)} строк.')

    def open_csv(self):
        p = filedialog.askopenfilename(filetypes=[('CSV', '*.csv'), ('Все файлы', '*.*')])
        if not p:
            return
        try:
            with open(p, encoding='utf-8-sig', newline='') as f:
                sample = f.read(4096)
                f.seek(0)
                delim = ';'
                try:
                    delim = csv.Sniffer().sniff(sample, delimiters=';,\t').delimiter
                except Exception:
                    pass
                reader = csv.reader(f, delimiter=delim)
                rows = list(reader)
            if not rows:
                raise ValueError('пустой файл')
            header = [h.strip().lower() for h in rows[0]]
            i_o = i_t = None
            if 'original' in header or 'оригинал' in header:
                i_o = header.index('original') if 'original' in header else header.index('оригинал')
                for name in ('translation', 'перевод', 'russian', 'русский'):
                    if name in header:
                        i_t = header.index(name)
                        break
                data = rows[1:]
            else:
                i_o, i_t = 0, 1
                data = rows
            table = {}
            skipped = 0
            for r in data:
                if len(r) <= max(i_o, i_t):
                    skipped += 1
                    continue
                table[r[i_o]] = r[i_t]
            self.translations = table
            self.refresh_table()
            self.log(f'CSV загружен: {len(table)} строк (пропущено битых: {skipped}).')
        except Exception as e:
            messagebox.showerror('Ошибка CSV', str(e))

    def save_csv(self):
        if not self.rows:
            messagebox.showinfo('Пусто', 'Сначала отсканируй игру (нужны скрипт/файл/счётчик).')
            return
        p = filedialog.asksaveasfilename(defaultextension='.csv', filetypes=[('CSV', '*.csv')])
        if not p:
            return
        with open(p, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f, delimiter=';')
            w.writerow(['original', 'translation', 'script', 'file', 'count'])
            for r in self.rows:
                w.writerow([r['text'], self.translations.get(r['text'], ''),
                            r['script'], r['file'], r['count']])
        self.log(f'CSV сохранён: {p}')

    def open_json(self):
        p = filedialog.askopenfilename(filetypes=[('JSON', '*.json'), ('Все файлы', '*.*')])
        if not p:
            return
        try:
            with open(p, encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.translations = {str(k): str(v) for k, v in data.items()}
            elif isinstance(data, list):
                self.translations = {str(r.get('original', r.get('key'))): str(r.get('translation', r.get('ru', '')))
                                     for r in data}
            else:
                raise ValueError('ожидался dict или list')
            self.refresh_table()
            self.log(f'JSON загружен: {len(self.translations)} строк.')
        except Exception as e:
            messagebox.showerror('Ошибка JSON', str(e))

    def save_json(self):
        p = filedialog.asksaveasfilename(defaultextension='.json', filetypes=[('JSON', '*.json')])
        if not p:
            return
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(self.translations, f, ensure_ascii=False, indent=1, sort_keys=True)
        self.log(f'JSON сохранён: {p}')

    def build(self):
        d = self.data_dir.get().strip()
        o = self.out_dir.get().strip()
        if not os.path.isdir(d):
            messagebox.showerror('Ошибка', 'Укажи папку *_Data ОРИГИНАЛЬНОЙ игры.')
            return
        if not o:
            messagebox.showerror('Ошибка', 'Укажи папку вывода.')
            return
        if not self.inventory:
            self.log('Скан не выполнен — выполняю автоматически, сборка продолжится после скана...')
            self._pending_build = (d, o)
            self.scan()
            return
        def job():
            return self._do_build(d, o)
        self.run_bg(job, ('build_done',))

    def _do_build(self, d, out):
        import UnityPy
        os.makedirs(out, exist_ok=True)
        tr = {k: v for k, v in self.translations.items() if v.strip() != ''}
        self.log(f'Переведённых строк в таблице: {len(tr)}.')
        files = {}
        for it in self.inventory:
            ru = tr.get(it['text'])
            if ru is None:
                continue
            files.setdefault(it['file'], {}).setdefault(it['pathID'], []).append(
                {'offset': it['offset'], 'text': it['text'], 'ru': ru})
        n_repl = sum(len(v) for f in files.values() for v in f.values())
        self.log(f'К замене: {n_repl} вхождений в {len(files)} файлах.')
        written = 0
        for fname, per_obj in sorted(files.items()):
            env = UnityPy.load(os.path.join(d, fname))
            changed = 0
            for obj in env.objects:
                entries = per_obj.get(obj.path_id)
                if not entries:
                    continue
                buf = bytearray(obj.get_raw_data())
                entries = sorted(entries, key=lambda e: e['offset'])
                delta = 0
                for e in entries:
                    off = e['offset'] + delta
                    splice_string(buf, off, e['text'], e['ru'])
                    n, = struct.unpack_from('<i', buf, off)
                    old_end = (off + 4 + len(e['text'].encode('utf-8')) + 3) & ~3
                    new_end = off + 4 + n + ((-(4 + n)) % 4)
                    delta += new_end - old_end
                obj.set_raw_data(bytes(buf))
                changed += 1
            data = env.file.save()
            with open(os.path.join(out, fname), 'wb') as f:
                f.write(data)
            written += 1
            self.log(f'  {fname}: объектов {changed}, строк '
                     f'{sum(len(v) for v in per_obj.values())}')
        for asset, pid, label in FONT_SLOTS:
            fpath = self.font_paths[label].get().strip()
            if not fpath or not os.path.isfile(fpath):
                self.log(f'  {asset}: {label} — оставлен родной.')
                continue
            src = os.path.join(out, asset)
            if not os.path.isfile(src):
                src = os.path.join(d, asset)
            env = UnityPy.load(src)
            ok = replace_font(env, pid, fpath)
            if ok:
                data = env.file.save()
                with open(os.path.join(out, asset), 'wb') as f:
                    f.write(data)
                self.log(f'  {asset}: шрифт {label} -> {os.path.basename(fpath)}')
                if asset not in files:
                    written += 1
        self.log(f'Готово. Патченных файлов: {written}, папка: {out}')
        self.log('Установка: скопируй файлы из папки вывода в *_Data игры с заменой.')
        return written

    def _on_build_done(self, result):
        self._set_busy(False)
        if result is not None:
            messagebox.showinfo('Готово', f'Собрано файлов: {result}.\nСкопируй их в *_Data игры с заменой.')

def main():
    app = App()
    app.mainloop()

if __name__ == '__main__':
    main()
