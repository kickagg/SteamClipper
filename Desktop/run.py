#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SteamClipper - versao desktop.

Janela unica com o mpv renderizando dentro do layout. O player recebe o HWND de um
frame do tkinter (opcao "wid" do mpv) e desenha ali: HEVC 1440p original, decodificado
por hardware, sem transcodificar nada.

Um servidor HTTP minimo roda numa thread so para alimentar o mpv com /api/stream - e
o que transforma os segmentos DASH num arquivo unico com seek instantaneo. Ele nao
serve interface nenhuma; a UI e nativa.
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
import tkinter as tk
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from steamclipper import Config, Jobs, Mpv, PRESETS, scan_sessions, strip, thumb, virtual  # noqa: E402
from steamclipper.steam import chunk_list                                          # noqa: E402

CFG = Config()
JOBS = Jobs(CFG)
PORT = Config.PORT + 1          # nao briga com a versao Browser

BG, PANEL, PANEL2 = "#0f1216", "#161b22", "#1c232c"
LINE, TX, TX2, TX3 = "#252d38", "#e6edf3", "#9aa7b4", "#6b7885"
AC, OK, WARN, ERR = "#4a9eff", "#3fb950", "#d29922", "#f85149"


# --------------------------------------------------- servidor so para o mpv
class Feed(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path != "/api/stream":
            self.send_response(404); self.send_header("Content-Length", "0")
            self.end_headers(); return
        q = parse_qs(u.query)
        vf = virtual(CFG, q["session"][0], int(q.get("stream", ["0"])[0]))
        total = vf.size

        start, end, code = 0, total - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), total - 1)
                elif m.group(2):
                    start = max(0, total - int(m.group(2)))
                code = 206
        self.send_response(code)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        try:
            for buf in vf.read(start, end):
                self.wfile.write(buf)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    do_HEAD = do_GET


class FeedServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


def start_feed() -> int:
    global PORT
    for port in range(PORT, PORT + 20):
        try:
            srv = FeedServer(("127.0.0.1", port), Feed)
        except OSError:
            continue
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        PORT = port
        return port
    raise RuntimeError("nenhuma porta livre para o feed do player")


def stream_url(sid: str, stream: int = 0) -> str:
    u = f"http://127.0.0.1:{PORT}/api/stream?session={sid}"
    return u + "&stream=1" if stream else u


# ------------------------------------------------------------------- helpers
def fmt(s: float) -> str:
    s = max(0, int(round(s)))
    h, m, x = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{x:02d}" if h else f"{m}:{x:02d}"


def parse_t(v: str):
    try:
        p = [float(x) for x in str(v).strip().split(":")]
    except ValueError:
        return None
    if len(p) == 3:
        return p[0] * 3600 + p[1] * 60 + p[2]
    if len(p) == 2:
        return p[0] * 60 + p[1]
    return p[0] if p else None


def gb(b: int) -> str:
    return f"{b / 1073741824:.2f} GB"


class Flat(tk.Label):
    """Botao chato o suficiente para combinar com o resto (tk.Button e feio no Win)."""

    def __init__(self, master, text, cmd=None, bg=PANEL2, fg=TX, pad=(12, 6),
                 font=None, **kw):
        super().__init__(master, text=text, bg=bg, fg=fg, padx=pad[0], pady=pad[1],
                         font=font, cursor="hand2", **kw)
        self._bg, self._cmd = bg, cmd
        self.bind("<Button-1>", lambda e: cmd and cmd())
        self.bind("<Enter>", lambda e: self.config(bg=self._hover()))
        self.bind("<Leave>", lambda e: self.config(bg=self._bg))

    def _hover(self):
        return "#2c3744" if self._bg == PANEL2 else "#2b7fd4" if self._bg == AC else self._bg

    def recolor(self, bg):
        self._bg = bg
        self.config(bg=bg)


# ------------------------------------------------------------------- app
class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.sessions = scan_sessions(CFG)
        self.cur = None
        self.preset = "deliver"
        self.inp, self.out = 0.0, 90.0
        self.pos = 0.0
        self.thumbs = {}          # tkinter descarta PhotoImage sem referencia viva
        self.strip_img = {}
        self.mpv = Mpv(CFG.libmpv)
        self.cards = []

        root.title("SteamClipper")
        root.geometry("1560x920")
        root.minsize(1180, 760)
        root.configure(bg=BG)

        self.f  = tkfont.Font(family="Segoe UI", size=10)
        self.fb = tkfont.Font(family="Segoe UI", size=10, weight="bold")
        self.fs = tkfont.Font(family="Segoe UI", size=9)
        self.fh = tkfont.Font(family="Segoe UI", size=14, weight="bold")
        self.fm = tkfont.Font(family="Consolas", size=10)

        self._header()
        body = tk.Frame(root, bg=BG); body.pack(fill="both", expand=True)
        self._sidebar(body)
        self._main(body)
        self._jobs_bar()

        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.bind("<space>", lambda e: self.toggle())
        root.bind("i", lambda e: self.mark_in())
        root.bind("o", lambda e: self.mark_out())
        root.bind("<Left>", lambda e: self.seek(self.pos - 5))
        root.bind("<Right>", lambda e: self.seek(self.pos + 5))

        if self.sessions:
            self.select(self.sessions[-1])
        self._tick()

    # ------------------------------------------------------------ layout
    def _header(self):
        h = tk.Frame(self.root, bg=PANEL, height=52); h.pack(fill="x"); h.pack_propagate(False)
        t = tk.Frame(h, bg=PANEL); t.pack(side="left", padx=18)
        tk.Label(t, text="Steam", bg=PANEL, fg=TX, font=self.fh).pack(side="left")
        tk.Label(t, text="Clipper", bg=PANEL, fg=AC, font=self.fh).pack(side="left")
        Flat(h, "Abrir pasta de saída", self.reveal, font=self.fs).pack(side="right", padx=(0, 16))
        Flat(h, "Recarregar", self.reload, font=self.fs).pack(side="right", padx=8)

    def _sidebar(self, body):
        sb = tk.Frame(body, bg=PANEL, width=310); sb.pack(side="left", fill="y")
        sb.pack_propagate(False)
        tk.Label(sb, text="GRAVAÇÕES", bg=PANEL, fg=TX3, font=self.fs,
                 anchor="w").pack(fill="x", padx=14, pady=(12, 6))

        wrap = tk.Frame(sb, bg=PANEL); wrap.pack(fill="both", expand=True)
        cv = tk.Canvas(wrap, bg=PANEL, highlightthickness=0, bd=0)
        sc = tk.Scrollbar(wrap, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=sc.set)
        cv.pack(side="left", fill="both", expand=True)
        sc.pack(side="right", fill="y")

        self.list_frame = tk.Frame(cv, bg=PANEL)
        win = cv.create_window((0, 0), window=self.list_frame, anchor="nw")

        # O frame interno precisa acompanhar a largura do canvas; sem isto ele
        # nasce com 1px e os cards ficam invisiveis.
        cv.bind("<Configure>", lambda e: cv.itemconfigure(win, width=e.width))
        self.list_frame.bind("<Configure>",
                             lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.bind_all("<MouseWheel>", lambda e: cv.yview_scroll(-e.delta // 120, "units"))
        self._fill_list()

    def _fill_list(self):
        for w in self.list_frame.winfo_children():
            w.destroy()
        self.cards = []
        for s in reversed(self.sessions):
            c = tk.Frame(self.list_frame, bg=PANEL, highlightthickness=1,
                         highlightbackground=PANEL, cursor="hand2")
            c.pack(fill="x", padx=10, pady=3)
            img = tk.Label(c, bg="#0a0d11", width=84, height=47)
            img.pack(side="left", padx=(8, 10), pady=8)
            self._load_thumb(img, s, s["seconds"] / 2, (84, 47))
            tx = tk.Frame(c, bg=PANEL); tx.pack(side="left", fill="x", expand=True, pady=8)
            tk.Label(tx, text=s["game"], bg=PANEL, fg=TX, font=self.fb,
                     anchor="w").pack(fill="x")
            line = f"{s['started_h']}   {fmt(s['seconds'])}"
            if s["pruned"]:
                line += f"   −{s['pruned_min']} min"
            tk.Label(tx, text=line, bg=PANEL, fg=TX2, font=self.fs,
                     anchor="w").pack(fill="x")
            tk.Label(tx, text=f"{s['width']}×{s['height']} · {gb(s['bytes'])}",
                     bg=PANEL, fg=TX3, font=self.fs, anchor="w").pack(fill="x")
            for w in (c, img, tx, *tx.winfo_children()):
                w.bind("<Button-1>", lambda e, x=s: self.select(x))
            self.cards.append((c, tx, s["id"]))

    def _load_thumb(self, label, s, t, size):
        """Gera a miniatura fora da thread da UI e injeta quando ficar pronta."""
        def work():
            try:
                p = thumb(CFG, s['id'], t, width=size[0]*2, fmt='png')
            except Exception:                                  # noqa: BLE001
                return
            def apply():
                if not label.winfo_exists():
                    return
                try:
                    img = tk.PhotoImage(file=str(p))
                    fx = max(1, img.width() // size[0])
                    fy = max(1, img.height() // size[1])
                    img = img.subsample(fx, fy)
                    label.config(image=img, width=size[0], height=size[1])
                    self.thumbs[id(label)] = img      # sem isto o GC apaga a imagem
                except tk.TclError:
                    pass
            self.root.after(0, apply)
        threading.Thread(target=work, daemon=True).start()

    def _main(self, body):
        mn = tk.Frame(body, bg=BG); mn.pack(side="left", fill="both", expand=True)

        self.title_lbl = tk.Label(mn, text="", bg=BG, fg=TX, font=self.fh, anchor="w")
        self.title_lbl.pack(fill="x", padx=20, pady=(14, 0))
        self.sub_lbl = tk.Label(mn, text="", bg=BG, fg=TX2, font=self.fs, anchor="w")
        self.sub_lbl.pack(fill="x", padx=20, pady=(2, 10))

        # O mpv desenha numa janela filha nativa: ela fica ACIMA de qualquer widget
        # tk na mesma area. Por isso o video tem a faixa dele e os controles ficam
        # fora, nunca por cima.
        self.video = tk.Frame(mn, bg="#07090c", height=430)
        self.video.pack(fill="both", expand=True, padx=20)
        self.video.pack_propagate(False)
        self.video_hint = tk.Label(self.video, text="Selecione uma gravação",
                                   bg="#07090c", fg=TX3, font=self.f)
        self.video_hint.place(relx=.5, rely=.5, anchor="center")

        ct = tk.Frame(mn, bg=PANEL2, height=48); ct.pack(fill="x", padx=20)
        ct.pack_propagate(False)
        self.play_btn = Flat(ct, "▶", self.toggle, pad=(14, 7), font=self.f)
        self.play_btn.pack(side="left", padx=(8, 10), pady=6)
        self.time_lbl = tk.Label(ct, text="0:00 / 0:00", bg=PANEL2, fg=TX, font=self.fm)
        self.time_lbl.pack(side="left")
        Flat(ct, "⏪", lambda: self.seek(self.pos - 5), pad=(10, 7), font=self.f).pack(side="left", padx=6)
        Flat(ct, "⏩", lambda: self.seek(self.pos + 5), pad=(10, 7), font=self.f).pack(side="left")
        self.speed = tk.StringVar(value="1")
        sp = tk.OptionMenu(ct, self.speed, "0.25", "0.5", "1", "2", "4",
                           command=lambda v: self.mpv.command("speed", v))
        sp.config(bg=PANEL2, fg=TX, font=self.fs, bd=0, highlightthickness=0,
                  activebackground="#2c3744", width=4)
        sp["menu"].config(bg=PANEL2, fg=TX, font=self.fs)
        sp.pack(side="left", padx=10)
        Flat(ct, "Fim ]", self.mark_out, font=self.fs).pack(side="right", padx=(4, 10))
        Flat(ct, "[ Início", self.mark_in, font=self.fs).pack(side="right", padx=4)

        self.tl = tk.Canvas(mn, bg="#0a0d11", height=56, highlightthickness=1,
                            highlightbackground=LINE, cursor="hand2")
        self.tl.pack(fill="x", padx=20, pady=(10, 0))
        self.tl.bind("<Button-1>", self._tl_down)
        self.tl.bind("<B1-Motion>", self._tl_drag)
        self.tl.bind("<ButtonRelease-1>", self._tl_up)
        self.tl.bind("<Configure>", lambda e: self._draw_tl())
        self.lbl_row = tk.Frame(mn, bg=BG); self.lbl_row.pack(fill="x", padx=20)
        self.t0_lbl = tk.Label(self.lbl_row, text="0:00", bg=BG, fg=TX3, font=self.fs)
        self.t0_lbl.pack(side="left")
        self.t1_lbl = tk.Label(self.lbl_row, text="", bg=BG, fg=TX3, font=self.fs)
        self.t1_lbl.pack(side="right")
        tk.Label(mn, text="Clique na linha do tempo para pular · arraste para marcar o trecho · "
                          "espaço play · i / o marcam início e fim · ← → 5s",
                 bg=BG, fg=TX3, font=self.fs, anchor="w").pack(fill="x", padx=20, pady=(6, 10))

        self._fields(mn)

    def _fields(self, mn):
        row = tk.Frame(mn, bg=BG); row.pack(fill="x", padx=20)
        self.e_in, self.e_out, self.e_dur = None, None, None
        for lbl, attr, ro in (("INÍCIO", "e_in", False), ("FIM", "e_out", False),
                              ("DURAÇÃO", "e_dur", True)):
            f = tk.Frame(row, bg=BG); f.pack(side="left", padx=(0, 12))
            tk.Label(f, text=lbl, bg=BG, fg=TX3, font=self.fs, anchor="w").pack(fill="x")
            e = tk.Entry(f, bg=PANEL2, fg=TX, font=self.fm, width=10, bd=0,
                         insertbackground=TX, justify="center",
                         disabledbackground=PANEL2, disabledforeground=TX2)
            e.pack(ipady=5)
            if ro:
                e.config(state="disabled")
            else:
                e.bind("<Return>", lambda ev: self._commit())
                e.bind("<FocusOut>", lambda ev: self._commit())
            setattr(self, attr, e)

        f = tk.Frame(row, bg=BG); f.pack(side="left", fill="x", expand=True, padx=(0, 12))
        tk.Label(f, text="NOME DO ARQUIVO", bg=BG, fg=TX3, font=self.fs,
                 anchor="w").pack(fill="x")
        self.e_name = tk.Entry(f, bg=PANEL2, fg=TX, font=self.f, bd=0,
                               insertbackground=TX)
        self.e_name.pack(fill="x", ipady=5)

        f = tk.Frame(row, bg=BG); f.pack(side="left")
        tk.Label(f, text="QUALIDADE", bg=BG, fg=TX3, font=self.fs, anchor="w").pack(fill="x")
        self.quality = tk.StringVar(value="19")
        q = tk.OptionMenu(f, self.quality, "15", "19", "23")
        q.config(bg=PANEL2, fg=TX, font=self.fs, bd=0, highlightthickness=0,
                 activebackground="#2c3744", width=5)
        q["menu"].config(bg=PANEL2, fg=TX, font=self.fs)
        q.pack(fill="x")

        pr = tk.Frame(mn, bg=BG); pr.pack(fill="x", padx=20, pady=(14, 0))
        self.preset_btns = {}
        for key, meta in PRESETS.items():
            b = tk.Frame(pr, bg=PANEL2, highlightthickness=1,
                         highlightbackground=AC if key == self.preset else LINE,
                         cursor="hand2")
            b.pack(side="left", fill="x", expand=True, padx=(0, 9))
            tk.Label(b, text=meta["label"], bg=PANEL2, fg=TX, font=self.fb,
                     anchor="w").pack(fill="x", padx=12, pady=(9, 0))
            tk.Label(b, text=meta["hint"], bg=PANEL2, fg=TX2, font=self.fs,
                     anchor="w", justify="left", wraplength=260).pack(fill="x", padx=12,
                                                                     pady=(2, 10))
            for w in (b, *b.winfo_children()):
                w.bind("<Button-1>", lambda e, k=key: self.set_preset(k))
            self.preset_btns[key] = b

        act = tk.Frame(mn, bg=BG); act.pack(fill="x", padx=20, pady=14)
        self.exp_btn = Flat(act, "Exportar trecho", self.export, bg=AC, fg="white",
                            pad=(20, 9), font=self.fb)
        self.exp_btn.pack(side="right")
        Flat(act, "Fechar player", self.close_player, font=self.fs).pack(side="right", padx=8)

    def _jobs_bar(self):
        self.jobs_frame = tk.Frame(self.root, bg=PANEL)
        self.jobs_lbl = tk.Label(self.jobs_frame, text="", bg=PANEL, fg=TX2,
                                 font=self.fs, anchor="w")
        self.jobs_lbl.pack(fill="x", padx=20, pady=7)

    # ---------------------------------------------------------- acoes
    def select(self, s):
        self.cur = s
        self.inp, self.out = 0.0, min(90.0, s["seconds"])
        self.pos = 0.0
        for c, tx, sid in self.cards:
            on = sid == s["id"]
            c.config(bg="#1a2836" if on else PANEL,
                     highlightbackground=AC if on else PANEL)
            tx.config(bg="#1a2836" if on else PANEL)
            for w in tx.winfo_children():
                w.config(bg="#1a2836" if on else PANEL)
        self.title_lbl.config(text=s["game"])
        sub = (f"{s['started_h']} · {fmt(s['seconds'])} disponíveis · "
               f"{s['width']}×{s['height']} · {gb(s['bytes'])} ({s['gb_h']} GB/h)")
        if s["pruned"]:
            sub += f" · o buffer apagou os primeiros {s['pruned_min']} min"
        self.sub_lbl.config(text=sub)
        self.t1_lbl.config(text=fmt(s["seconds"]))
        stamp = s["started_h"].replace("/", "-").replace(":", "-").replace(" ", "_")
        clean = re.sub(r'[<>:"/\\|?*™®]', "", s["game"]).strip()
        self.e_name.delete(0, "end"); self.e_name.insert(0, f"{clean} {stamp}")
        self.video_hint.config(text="Abrindo no player…")
        self._sync(); self._draw_tl()
        self._load_strip(s)
        self.open_player(0)

    def _load_strip(self, s):
        """Faixa de quadros da linha do tempo, gerada fora da thread da UI."""
        if s["id"] in self.strip_img:
            return
        def work():
            try:
                self.tl.update_idletasks()
                w = max(400, self.tl.winfo_width())
                p = strip(CFG, s["id"], 16, width=max(60, w // 16), fmt="png")
            except Exception:                                  # noqa: BLE001
                return
            def apply():
                try:
                    img = tk.PhotoImage(file=str(p))
                    fy = max(1, img.height() // 56)
                    self.strip_img[s["id"]] = img.subsample(1, fy)
                    if self.cur and self.cur["id"] == s["id"]:
                        self._draw_tl()
                except tk.TclError:
                    pass
            self.root.after(0, apply)
        threading.Thread(target=work, daemon=True).start()

    def open_player(self, t):
        if not self.mpv.available:
            self.video_hint.config(text="libmpv não encontrada — instale o mpv.net")
            return
        sid = self.cur["id"]
        has_audio = bool(chunk_list(CFG.video_dir / sid, 1))
        self.video.update_idletasks()
        if self.mpv.h is None:
            self.mpv.wid = self.video.winfo_id()
        def work():
            try:
                self.mpv.open(stream_url(sid), t,
                              stream_url(sid, 1) if has_audio else None,
                              title=self.cur["game"])
                self.root.after(0, lambda: self.video_hint.place_forget())
            except Exception as e:                             # noqa: BLE001
                self.root.after(0, lambda: self.video_hint.config(text=f"erro: {e}"))
        threading.Thread(target=work, daemon=True).start()

    def close_player(self):
        self.mpv.destroy()
        self.video_hint.config(text="Player fechado — clique na linha do tempo para reabrir")
        self.video_hint.place(relx=.5, rely=.5, anchor="center")

    def toggle(self):
        if self.mpv.h is None:
            self.open_player(self.pos)
        else:
            self.mpv.command("toggle")

    def seek(self, t):
        if not self.cur:
            return
        self.pos = max(0, min(self.cur["seconds"] - 0.5, t))
        if self.mpv.h is None:
            self.open_player(self.pos)
        else:
            self.mpv.command("seek", self.pos)
        self._draw_tl()

    def mark_in(self):
        self.inp = self.pos
        if self.out <= self.inp:
            self.out = min(self.cur["seconds"], self.inp + 30)
        self._sync(); self._draw_tl()

    def mark_out(self):
        self.out = max(self.inp + 1, self.pos)
        self._sync(); self._draw_tl()

    def set_preset(self, key):
        self.preset = key
        for k, b in self.preset_btns.items():
            b.config(highlightbackground=AC if k == key else LINE)

    def reload(self):
        self.sessions = scan_sessions(CFG)
        self._fill_list()

    def reveal(self):
        CFG.output.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["explorer", str(CFG.output)])

    def export(self):
        if not self.cur:
            return
        dur = max(0.5, self.out - self.inp)
        JOBS.submit(self.cur["id"], self.preset, self.inp, dur,
                    int(self.quality.get()), self.e_name.get().strip() or self.cur["game"])
        self.jobs_frame.pack(fill="x", side="bottom")

    def quit(self):
        self.mpv.destroy()
        self.root.destroy()

    # ------------------------------------------------------- timeline
    def _tl_pos(self, e):
        w = max(1, self.tl.winfo_width())
        return max(0, min(1, e.x / w)) * self.cur["seconds"]

    def _tl_down(self, e):
        if not self.cur:
            return
        self._down = self._tl_pos(e); self._moved = False

    def _tl_drag(self, e):
        if not self.cur or not hasattr(self, "_down"):
            return
        t = self._tl_pos(e)
        if abs(t - self._down) > self.cur["seconds"] * 0.004:
            self._moved = True
        if self._moved:
            self.inp, self.out = min(self._down, t), max(self._down, t)
            self._sync(); self._draw_tl()

    def _tl_up(self, e):
        if not self.cur or not hasattr(self, "_down"):
            return
        if not self._moved:
            self.seek(self._tl_pos(e))
        del self._down

    def _draw_tl(self):
        c = self.tl
        c.delete("all")
        if not self.cur:
            return
        w, h = max(1, c.winfo_width()), 56
        total = self.cur["seconds"]
        img = self.strip_img.get(self.cur["id"])
        if img:
            c.create_image(0, 0, image=img, anchor="nw")
        else:
            for i in range(16):
                c.create_rectangle(i * w / 16, 0, (i + 1) * w / 16, h,
                                   fill="#12171d" if i % 2 else "#161c23", outline="")
        x0, x1 = self.inp / total * w, self.out / total * w
        c.create_rectangle(x0, 0, x1, h, fill="#1d3a5c", outline=AC, width=2)
        for mk in self.cur["markers"]:
            x = mk["t"] / total * w
            c.create_line(x, 0, x, 10, fill=WARN, width=2)
        x = self.pos / total * w
        c.create_line(x, 0, x, h, fill="white", width=2)

    def _sync(self):
        self.e_in.delete(0, "end"); self.e_in.insert(0, fmt(self.inp))
        self.e_out.delete(0, "end"); self.e_out.insert(0, fmt(self.out))
        self.e_dur.config(state="normal")
        self.e_dur.delete(0, "end"); self.e_dur.insert(0, fmt(self.out - self.inp))
        self.e_dur.config(state="disabled")

    def _commit(self):
        if not self.cur:
            return
        a, b = parse_t(self.e_in.get()), parse_t(self.e_out.get())
        if a is not None:
            self.inp = max(0, min(self.cur["seconds"], a))
        if b is not None:
            self.out = max(self.inp + 1, min(self.cur["seconds"], b))
        self._sync(); self._draw_tl()

    # ------------------------------------------------------------ loop
    def _tick(self):
        st = self.mpv.state()
        if st.get("open"):
            self.pos = st["pos"]
            self.play_btn.config(text="▶" if st["paused"] else "⏸")
            if self.cur:
                self.time_lbl.config(text=f"{fmt(self.pos)} / {fmt(self.cur['seconds'])}")
            self._draw_tl()

        jobs = JOBS.snapshot()
        if jobs:
            act = [j for j in jobs.values() if j["status"] not in ("pronto", "erro")]
            if act:
                j = act[0]
                self.jobs_lbl.config(text=f"{j['label']} · {j['status']} {j.get('pct',0)}%",
                                     fg=AC)
            else:
                last = list(jobs.values())[-1]
                if last["status"] == "pronto":
                    self.jobs_lbl.config(
                        text=f"{last['label']} · pronto · "
                             f"{last.get('size',0)/1048576:.1f} MB → {CFG.output}", fg=OK)
                else:
                    self.jobs_lbl.config(text=f"{last['label']} · erro: "
                                              f"{last.get('error','')}", fg=ERR)
            self.jobs_frame.pack(fill="x", side="bottom")
        self.root.after(400, self._tick)


def main():
    if not CFG.ok:
        r = tk.Tk(); r.withdraw()
        messagebox.showerror("SteamClipper", "\n".join(CFG.problems()) or
                             "Nenhuma gravacao encontrada.")
        return
    start_feed()
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:                                          # noqa: BLE001
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
