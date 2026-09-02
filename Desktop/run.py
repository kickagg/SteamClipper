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
from tkinter import filedialog, messagebox
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from steamclipper import (AUDIO_CHOICES, CODECS, CONTAINERS, FPS_CHOICES,
                          PRESETS, SCALES,
                          Config, Jobs, Mpv, estimate_mb, preset_opts,
                          scan_sessions, thumb, virtual, waveform)  # noqa: E402
from steamclipper.export import (CUSTOM, CUSTOM_META, MIN_DURATION,
                                 format_estimate, human_mb, output_name,
                                 timecode)  # noqa: E402
from steamclipper.steam import chunk_list                                          # noqa: E402

CFG = Config()
JOBS = Jobs(CFG)
PORT = Config.PORT + 1          # nao briga com a versao Browser

BG, PANEL, PANEL2 = "#0f1216", "#161b22", "#1c232c"
LINE, TX, TX2, TX3 = "#252d38", "#e6edf3", "#9aa7b4", "#6b7885"
AC, OK, WARN, ERR = "#4a9eff", "#3fb950", "#d29922", "#f85149"

TILE_H = 56                       # altura da faixa da linha do tempo
TILE_W = round(TILE_H * 16 / 9)   # 16:9, para o quadro nao distorcer
RULER_H = 16                      # regua de tempo acima da faixa
WAVE_H = 46                       # altura da forma de onda
LINE2 = "#39414F"

# Passos "redondos" da regua, do segundo a meia hora. O desenho escolhe o
# primeiro que der pelo menos ~110px entre marcas no zoom atual.
RULER_STEPS = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)

# CQ do NVENC com nome: o numero sozinho nao diz nada a quem so quer exportar.
QUALITY_LEVELS = {15: "Máxima (15)", 19: "Alta (19)", 23: "Média (23)",
                  27: "Econômica (27)", 32: "Compressão forte (32)"}


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


def _nearest_quality(q: int) -> int:
    return min(QUALITY_LEVELS, key=lambda k: abs(k - q))


def gb(b: int) -> str:
    return f"{b / 1073741824:.2f} GB"


class Tooltip:
    """Balao de ajuda simples - o tk nao traz nada parecido de fabrica."""

    def __init__(self, widget, text, delay=450):
        self.w, self.text, self.delay = widget, text, delay
        self.tip = None
        self.job = None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._leave, add="+")
        widget.bind("<ButtonPress>", self._leave, add="+")

    def _enter(self, _e=None):
        self.job = self.w.after(self.delay, self._show)

    def _leave(self, _e=None):
        if self.job:
            self.w.after_cancel(self.job); self.job = None
        if self.tip:
            self.tip.destroy(); self.tip = None

    def _show(self):
        if self.tip:
            return
        x = self.w.winfo_rootx() + self.w.winfo_width() // 2
        y = self.w.winfo_rooty() - 6
        self.tip = tw = tk.Toplevel(self.w)
        tw.wm_overrideredirect(True)
        tw.configure(bg=LINE)
        tk.Label(tw, text=self.text, bg="#0b0e12", fg=TX, padx=9, pady=5,
                 font=("Segoe UI", 9), justify="left").pack(padx=1, pady=1)
        tw.update_idletasks()
        tw.wm_geometry(f"+{x - tw.winfo_width() // 2}+{y - tw.winfo_height()}")


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


class CustomDialog(tk.Toplevel):
    """Configuracao do preset Personalizado.

    Nao exporta: ajusta e aplica. Quem renderiza e sempre o botao Exportar trecho,
    para que o fluxo tenha um unico caminho de saida.
    """

    def __init__(self, app, current: dict | None = None):
        super().__init__(app.root)
        self.app = app
        self.session = app.cur
        self.start, self.dur = app.inp, max(0.5, app.out - app.inp)
        self.result = None
        self.title("Configuração personalizada")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(app.root)

        o = dict(current) if current else preset_opts("deliver")
        self.codec = tk.StringVar(value=o["codec"])
        self.container = tk.StringVar(value=o["container"])
        self.scale = tk.StringVar(value=str(o.get("scale", 0)))
        self.fps = tk.StringVar(value=str(o.get("fps", 60)))
        self.quality = tk.IntVar(value=o.get("quality", 19))
        self.bitrate = tk.StringVar(value="")
        self.audio = tk.StringVar(value=o.get("audio", "keep"))
        self.outdir = tk.StringVar(value=str(CFG.output))
        self.menus = []          # (var, wrap) para _refresh_menus
        self.fname = tk.StringVar(value=app.e_name.get().strip())

        pad = {"padx": 22}
        tk.Label(self, text="Configuração personalizada", bg=BG, fg=TX, font=app.fh,
                 anchor="w").pack(fill="x", pady=(18, 2), **pad)
        tk.Label(self, text="Ajuste e aplique. A exportação continua no botão "
                            "“Exportar trecho”.",
                 bg=BG, fg=TX2, font=app.fs, anchor="w").pack(fill="x", pady=(0, 4), **pad)

        tk.Frame(self, bg=LINE, height=1).pack(fill="x", pady=16, **pad)

        # ---- ajustes finos
        grid = tk.Frame(self, bg=BG); grid.pack(fill="x", **pad)
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)

        def row(r, c, label, widget, tip):
            tk.Label(grid, text=label, bg=BG, fg=TX3, font=app.fs,
                     anchor="w").grid(row=r, column=c, sticky="w", pady=(0, 2))
            widget.grid(row=r + 1, column=c, sticky="ew", padx=(0, 14), pady=(0, 12))
            Tooltip(widget, tip)

        row(0, 0, "CODEC", self._menu(grid, self.codec, list(CODECS),
                                      lambda v: (self._sync_container(), self._est()),
                                      lambda k: CODECS[k]),
            "H.264 abre em qualquer lugar. H.265 gera metade do tamanho mas\n"
            "alguns players e editores antigos não reconhecem.\n"
            "Cópia não reprocessa: instantâneo, porém mantém o VFR.")
        row(0, 2, "FORMATO", self._menu(grid, self.container, list(CONTAINERS),
                                        lambda v: None, lambda k: CONTAINERS[k]),
            "MP4 é o mais compatível. MKV aceita qualquer codec.\n"
            "MOV é o padrão para DNxHR na edição.")
        row(2, 0, "RESOLUÇÃO", self._menu(grid, self.scale, [str(k) for k in SCALES],
                                          lambda v: self._est(),
                                          lambda k: SCALES[int(k)]),
            "Reduzir a resolução é o jeito mais eficaz de encolher o arquivo.\n"
            "Original mantém 2560×1440 da gravação.")
        row(2, 2, "TAXA DE QUADROS", self._menu(grid, self.fps,
                                                [str(k) for k in FPS_CHOICES],
                                                lambda v: self._est(),
                                                lambda k: FPS_CHOICES[int(k)]),
            "60 fps preserva a fluidez do jogo.\n"
            "30 fps corta o tamanho quase pela metade.\n"
            "Original mantém o framerate variável — evite para editar.")

        # O tk.Scale fica quebrado no Windows com tema escuro; um menu nomeado
        # ainda comunica melhor que um numero solto.
        self.qvar = tk.StringVar(value=str(self.quality.get()))
        self.qmenu = self._menu(grid, self.qvar, [str(k) for k in QUALITY_LEVELS],
                                lambda v: (self.quality.set(int(v)), self._est()),
                                lambda k: QUALITY_LEVELS[int(k)])
        row(4, 0, "QUALIDADE", self.qmenu,
            "CQ do NVENC: menor = melhor imagem e arquivo maior.\n"
            "Máxima ≈ sem perda visível · Alta é o padrão para postar.\n"
            "Ignorado quando você define um bitrate fixo abaixo.")

        bf = tk.Frame(grid, bg=BG)
        self.e_br = tk.Entry(bf, textvariable=self.bitrate, bg=PANEL2, fg=TX,
                             font=app.fm, bd=0, width=10, insertbackground=TX,
                             justify="center")
        self.e_br.pack(side="left", ipady=4)
        self.e_br.bind("<KeyRelease>", lambda e: self._est())
        tk.Label(bf, text="kbps  (vazio = automático)", bg=BG, fg=TX3,
                 font=app.fs).pack(side="left", padx=8)
        row(4, 2, "BITRATE FIXO", bf,
            "Deixe vazio para o encoder escolher pela qualidade.\n"
            "Preencha quando precisar de um tamanho previsível —\n"
            "ex.: 8000 kbps ≈ 60 MB por minuto.")

        row(6, 0, "ÁUDIO", self._menu(grid, self.audio, list(AUDIO_CHOICES),
                                      lambda v: self._est(),
                                      lambda k: AUDIO_CHOICES[k]),
            "O Steam grava jogo, Discord e microfone somados numa faixa só,\n"
            "então não há fontes para separar aqui.\n\n"
            "Normalizar nivela o volume em −14 LUFS, o alvo do YouTube e da\n"
            "Twitch: suas gravações variam de −17 a −29 LUFS entre trechos,\n"
            "e isso deixa todas parecidas sem estourar o pico.\n"
            "Sem áudio remove a faixa por completo.")

        # ---- destino
        tk.Frame(self, bg=LINE, height=1).pack(fill="x", pady=(4, 16), **pad)
        df = tk.Frame(self, bg=BG); df.pack(fill="x", **pad)
        tk.Label(df, text="NOME DO ARQUIVO", bg=BG, fg=TX3, font=app.fs,
                 anchor="w").pack(fill="x")
        tk.Entry(df, textvariable=self.fname, bg=PANEL2, fg=TX, font=app.f, bd=0,
                 insertbackground=TX).pack(fill="x", ipady=5, pady=(2, 12))
        tk.Label(df, text="SALVAR EM", bg=BG, fg=TX3, font=app.fs,
                 anchor="w").pack(fill="x")
        pf = tk.Frame(df, bg=BG); pf.pack(fill="x", pady=(2, 0))
        e = tk.Entry(pf, textvariable=self.outdir, bg=PANEL2, fg=TX2, font=app.fs,
                     bd=0, insertbackground=TX)
        e.pack(side="left", fill="x", expand=True, ipady=5)
        b = Flat(pf, "Procurar…", self._browse, font=app.fs)
        b.pack(side="left", padx=(8, 0))
        Tooltip(b, "Escolhe a pasta de destino e guarda como padrão")

        self.est_lbl = tk.Label(self, text="", bg=BG, fg=TX2, font=app.fs, anchor="w")
        self.est_lbl.pack(fill="x", pady=(16, 0), **pad)

        act = tk.Frame(self, bg=BG); act.pack(fill="x", pady=(12, 20), **pad)
        Flat(act, "Aplicar", self._ok, bg=AC, fg="white", pad=(22, 9),
             font=app.fb).pack(side="right")
        Flat(act, "Cancelar", self.destroy, pad=(18, 9), font=app.f).pack(side="right", padx=8)

        self._sync_container()
        self._est()
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Return>", lambda e: self._ok())
        self.update_idletasks()
        # Centraliza na TELA e prende dentro dela: centralizar pela janela pai
        # jogava o rodape (e o botao) para fora quando a janela estava baixa.
        w, h = self.winfo_width(), self.winfo_height()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, min((sh - h) // 2, sh - h - 40))
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _menu(self, parent, var, values, on_change, fmt_fn):
        wrap = tk.Frame(parent, bg=PANEL2)
        lbl = tk.Label(wrap, text=fmt_fn(var.get()), bg=PANEL2, fg=TX,
                       font=self.app.fs, anchor="w", padx=10, pady=6, cursor="hand2")
        lbl.pack(fill="x")
        menu = tk.Menu(wrap, tearoff=0, bg=PANEL2, fg=TX, font=self.app.fs,
                       activebackground=AC, activeforeground="white", bd=0)
        for v in values:
            menu.add_command(label=fmt_fn(v),
                             command=lambda x=v: (var.set(x),
                                                  lbl.config(text=fmt_fn(x)),
                                                  on_change(x)))
        lbl.bind("<Button-1>",
                 lambda e: menu.tk_popup(lbl.winfo_rootx(),
                                         lbl.winfo_rooty() + lbl.winfo_height()))
        wrap._label, wrap._fmt = lbl, fmt_fn
        self.menus.append((var, wrap))
        return wrap

    def _refresh_menus(self):
        """Reescreve o rotulo de cada menu depois que um preset troca as opcoes."""
        for var, wrap in self.menus:
            wrap._label.config(text=wrap._fmt(var.get()))

    def _sync_container(self):
        """Combinacoes que nao fazem sentido: DNxHR em MP4, copia em MOV."""
        c = self.codec.get()
        forced = {"dnxhr": "mov"}.get(c)
        if forced and self.container.get() != forced:
            self.container.set(forced)
        on = c in ("h264", "hevc")
        self.qmenu._label.config(fg=TX if on else TX3, cursor="hand2" if on else "")
        self.e_br.config(state="normal" if on else "disabled")

    def _opts(self) -> dict:
        try:
            br = int(self.bitrate.get().strip() or 0)
        except ValueError:
            br = 0
        return {"codec": self.codec.get(), "container": self.container.get(),
                "scale": int(self.scale.get()), "fps": int(self.fps.get()),
                "quality": int(self.quality.get()), "bitrate": br,
                "audio": self.audio.get(), "outdir": self.outdir.get().strip()}

    def _est(self, *_):
        o = self._opts()
        src = self.session["bytes"] / self.session["seconds"] / 1048576
        self.est_lbl.config(
            text=f"Tamanho provável: {format_estimate(o, self.dur, src)}"
                 f"   ·   {fmt(self.dur)} de vídeo\n"
                 "A faixa é ampla porque o tamanho depende do movimento na cena.")


    def _browse(self):
        d = filedialog.askdirectory(parent=self, initialdir=self.outdir.get(),
                                    title="Pasta para os arquivos exportados")
        if d:
            self.outdir.set(str(Path(d)))

    def _ok(self):
        o = self._opts()
        if o["outdir"]:
            destino = Path(o["outdir"])
            if not destino.exists():
                if not messagebox.askyesno(
                        "SteamClipper",
                        "A pasta não existe:\n" + str(destino) + "\n\nCriar agora?",
                        parent=self):
                    return
            try:
                CFG.set_output(o["outdir"])       # vira o padrao das proximas vezes
            except OSError as e:
                messagebox.showerror("SteamClipper",
                                     "Pasta inválida:" + chr(10) + str(e),
                                     parent=self)
                return
        name = self.fname.get().strip()
        if name:
            self.app.e_name.delete(0, "end")
            self.app.e_name.insert(0, name)
        self.result = o
        self.destroy()


class RenderDialog(tk.Toplevel):
    """Painel de renderizacao: o que esta sendo gerado, quanto falta e cancelar."""

    def __init__(self, app, job_id):
        super().__init__(app.root)
        self.app, self.jid = app, job_id
        self.done = False
        self.title("Renderizando")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(app.root)

        j = JOBS.get(job_id)
        pad = {"padx": 22}

        self.head = tk.Label(self, text="Renderizando trecho", bg=BG, fg=TX,
                             font=app.fh, anchor="w")
        self.head.pack(fill="x", pady=(18, 2), **pad)
        self.file_lbl = tk.Label(self, text=j.get("label", ""), bg=BG, fg=TX2,
                                 font=app.fs, anchor="w")
        self.file_lbl.pack(fill="x", pady=(0, 14), **pad)

        box = tk.Frame(self, bg=PANEL2); box.pack(fill="x", **pad)
        self.rows = {}
        for key, label in (("summary", "CONFIGURAÇÃO"), ("trecho", "TRECHO"),
                           ("size", "TAMANHO"), ("dest", "DESTINO")):
            r = tk.Frame(box, bg=PANEL2); r.pack(fill="x", padx=14, pady=(9, 0))
            tk.Label(r, text=label, bg=PANEL2, fg=TX3, font=app.fs, width=13,
                     anchor="w").pack(side="left")
            v = tk.Label(r, text="", bg=PANEL2, fg=TX, font=app.fs, anchor="w",
                         justify="left", wraplength=330)
            v.pack(side="left", fill="x", expand=True)
            self.rows[key] = v
        tk.Frame(box, bg=PANEL2, height=10).pack()

        self.bar = tk.Canvas(self, height=8, bg="#232c37", highlightthickness=0)
        self.bar.pack(fill="x", pady=(16, 6), **pad)
        line = tk.Frame(self, bg=BG); line.pack(fill="x", **pad)
        self.pct_lbl = tk.Label(line, text="0%", bg=BG, fg=AC, font=app.fb)
        self.pct_lbl.pack(side="left")
        self.eta_lbl = tk.Label(line, text="calculando…", bg=BG, fg=TX2, font=app.fs)
        self.eta_lbl.pack(side="right")

        self.act = tk.Frame(self, bg=BG); self.act.pack(fill="x", pady=(18, 20), **pad)
        self.btn_main = Flat(self.act, "Cancelar", self._cancel, pad=(20, 9),
                             font=app.f)
        self.btn_main.pack(side="right")
        self.btn_alt = Flat(self.act, "Abrir pasta", app.reveal, pad=(18, 9),
                            font=app.f)

        self._fill(j)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda e: self._close())
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}"
                      f"+{max(0, min((sh - h) // 2, sh - h - 40))}")
        self._tick()

    def _fill(self, j):
        o = j.get("opts", {})
        self.rows["summary"].config(text=j.get("summary", ""))
        start = j.get("start", self.app.inp)
        self.rows["trecho"].config(
            text=f"{fmt(start)} → {fmt(start + j.get('seconds', 0))}"
                 f"  ({fmt(j.get('seconds', 0))})")
        self.rows["dest"].config(text=str(o.get("outdir") or CFG.output))

    def _cancel(self):
        if self.done:
            return
        JOBS.cancel(self.jid)

    def _close(self):
        # Fechar durante a renderizacao nao cancela: o trabalho continua e o
        # progresso volta a aparecer na barra inferior.
        self.destroy()
        self.app.render_dlg = None

    def _tick(self):
        if not self.winfo_exists():
            return
        j = JOBS.get(self.jid)
        if not j:
            return self._close()
        st = j.get("status", "")
        pct = j.get("pct", 0)

        w = max(1, self.bar.winfo_width())
        color = {"pronto": OK, "erro": ERR, "cancelado": WARN}.get(st, AC)
        self.bar.delete("all")
        self.bar.create_rectangle(0, 0, w * max(0, min(100, pct)) / 100, 8,
                                  fill=color, outline="")

        now = j.get("bytes_now", 0) / 1048576
        est = j.get("estimate_mb", 0)
        if st == "pronto":
            self.rows["size"].config(text=f"{j.get('size', 0) / 1048576:.1f} MB")
        elif now:
            self.rows["size"].config(text=f"{now:.1f} MB de ~{est:.0f} MB previstos")
        else:
            self.rows["size"].config(text=f"~{est:.0f} MB previstos")

        if st in ("pronto", "erro", "cancelado"):
            self.done = True
            self.pct_lbl.config(
                text={"pronto": "Concluído", "erro": "Falhou",
                      "cancelado": "Cancelado"}[st], fg=color)
            self.head.config(text={"pronto": "Renderização concluída",
                                   "erro": "A renderização falhou",
                                   "cancelado": "Renderização cancelada"}[st])
            took = j.get("elapsed", 0)
            self.eta_lbl.config(
                text=(f"em {fmt(took)}" if st == "pronto" else
                      j.get("error", "arquivo parcial removido")[:60]))
            self.btn_main.config(text="Fechar")
            self.btn_main._cmd = self._close
            self.btn_main.bind("<Button-1>", lambda e: self._close())
            if st == "pronto":
                self.btn_alt.pack(side="right", padx=8)
            return
        self.pct_lbl.config(text=f"{pct}%", fg=AC)
        eta = j.get("eta")
        speed = j.get("speed")
        parts = []
        if eta is not None:
            parts.append(f"faltam ~{fmt(eta)}")
        if speed:
            parts.append(f"{speed:.1f}× tempo real")
        self.eta_lbl.config(text=" · ".join(parts) or f"{st}…")
        self.after(400, self._tick)


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
        self.tiles = {}           # quadros da linha do tempo
        self.waves = {}           # picos de audio por sessao
        self.playing = False      # o cursor so puxa a vista tocando
        self.zoom = 1.0           # 1 = gravacao inteira na tela
        self.view0 = 0.0          # inicio da janela visivel, em segundos
        self._tile_times = []
        self._tl_job = None
        self._fs = None           # janela de tela cheia do player
        self.dlg = None           # instancia unica do CustomDialog
        self.render_dlg = None    # painel de renderizacao
        self.custom_opts = CFG.settings.get('custom_opts') or {}
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
        # PanedWindow no lugar de frames fixos: as divisorias viram alcas que o
        # usuario arrasta para dar mais espaco a lista ou ao video.
        body = tk.PanedWindow(root, orient="horizontal", bg=LINE, bd=0,
                              sashwidth=6, sashrelief="flat",
                              handlesize=0, opaqueresize=False)
        body.pack(fill="both", expand=True)
        self._sidebar(body)
        self._main(body)
        self._jobs_bar()

        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.bind("<space>", lambda e: self.toggle())
        root.bind("i", lambda e: self.mark_in())
        root.bind("o", lambda e: self.mark_out())
        root.bind("f", lambda e: self.fullscreen())
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

    def _sidebar(self, paned):
        sb = tk.Frame(paned, bg=PANEL)
        paned.add(sb, minsize=210, width=310, stretch="never")
        tk.Label(sb, text="GRAVAÇÕES", bg=PANEL, fg=TX3, font=self.fs,
                 anchor="w").pack(fill="x", padx=14, pady=(12, 6))

        wrap = tk.Frame(sb, bg=PANEL); wrap.pack(fill="both", expand=True)
        cv = tk.Canvas(wrap, bg=PANEL, highlightthickness=0, bd=0)
        self.list_sb = tk.Scrollbar(wrap, orient="vertical", command=cv.yview)
        cv.configure(yscrollcommand=self.list_sb.set)
        cv.pack(side="left", fill="both", expand=True)
        self.list_cv = cv

        self.list_frame = tk.Frame(cv, bg=PANEL)
        win = cv.create_window((0, 0), window=self.list_frame, anchor="nw")

        # O frame interno precisa acompanhar a largura do canvas; sem isto ele
        # nasce com 1px e os cards ficam invisiveis.
        cv.bind("<Configure>", lambda e: (cv.itemconfigure(win, width=e.width),
                                          self._sync_scroll()))
        self.list_frame.bind("<Configure>", lambda e: self._sync_scroll())

        # Roda so quando o ponteiro esta sobre a lista E ha o que rolar - antes o
        # bind_all capturava a roda da janela inteira e rolava mesmo com 1 card.
        cv.bind("<Enter>", lambda e: cv.bind_all("<MouseWheel>", self._wheel))
        cv.bind("<Leave>", lambda e: cv.unbind_all("<MouseWheel>"))
        self._fill_list()

    def _sync_scroll(self):
        """Some com a barra e desliga a roda quando tudo cabe na tela."""
        cv = self.list_cv
        cv.configure(scrollregion=cv.bbox("all"))
        need = self.list_frame.winfo_reqheight() > cv.winfo_height()
        if need and not self.list_sb.winfo_ismapped():
            self.list_sb.pack(side="right", fill="y")
        elif not need and self.list_sb.winfo_ismapped():
            self.list_sb.pack_forget()
            cv.yview_moveto(0)

    def _wheel(self, e):
        cv = self.list_cv
        if self.list_frame.winfo_reqheight() > cv.winfo_height():
            cv.yview_scroll(-e.delta // 120, "units")

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

    def _main(self, paned):
        # Divisoria vertical: o usuario decide quanto espaco o video toma em
        # relacao aos controles e presets.
        mn = tk.PanedWindow(paned, orient="vertical", bg=LINE, bd=0,
                            sashwidth=6, sashrelief="flat", handlesize=0,
                            opaqueresize=False)
        paned.add(mn, minsize=520, stretch="always")

        top = tk.Frame(mn, bg=BG)
        mn.add(top, minsize=220, height=520, stretch="always")

        self.title_lbl = tk.Label(top, text="", bg=BG, fg=TX, font=self.fh, anchor="w")
        self.title_lbl.pack(fill="x", padx=20, pady=(12, 0))
        self.sub_lbl = tk.Label(top, text="", bg=BG, fg=TX2, font=self.fs, anchor="w")
        self.sub_lbl.pack(fill="x", padx=20, pady=(2, 8))

        # O mpv desenha numa janela filha nativa: ela fica ACIMA de qualquer widget
        # tk na mesma area. Por isso os controles ficam fora, nunca por cima.
        # O wrap ocupa o espaco livre e o frame do video e centralizado nele com a
        # proporcao da gravacao, para nao esticar a imagem.
        self.video_wrap = tk.Frame(top, bg="#07090c")
        self.video_wrap.pack(fill="both", expand=True, padx=20)
        self.video = tk.Frame(self.video_wrap, bg="#07090c")
        self.video.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.video_wrap.bind("<Configure>", lambda e: self._fit_video())
        self.video_hint = tk.Label(self.video_wrap, text="Selecione uma gravação",
                                   bg="#07090c", fg=TX3, font=self.f)
        self.video_hint.place(relx=.5, rely=.5, anchor="center")

        bot = tk.Frame(mn, bg=BG)
        mn.add(bot, minsize=250, stretch="never")
        self._controls(bot)
        self._timeline(bot)
        self._fields(bot)

    def _controls(self, parent):
        ct = tk.Frame(parent, bg=PANEL2, height=48)
        ct.pack(fill="x", padx=20, pady=(8, 0))
        ct.pack_propagate(False)

        # Um frame centralizado: os controles de transporte ficam no meio, como
        # em qualquer player, em vez de empilhados a esquerda.
        mid = tk.Frame(ct, bg=PANEL2)
        mid.place(relx=.5, rely=.5, anchor="center")

        def ico(txt, cmd, tip, w=(11, 7)):
            b = Flat(mid, txt, cmd, pad=w, font=self.f)
            b.pack(side="left", padx=3)
            Tooltip(b, tip)
            return b

        ico("⏮", lambda: self.seek(self.inp), "Ir para o início marcado")
        ico("⏪", lambda: self.seek(self.pos - 10), "Voltar 10s")
        ico("◀", lambda: self.seek(self.pos - 5), "Voltar 5s  (←)")
        self.play_btn = ico("▶", self.toggle, "Reproduzir / pausar  (espaço)", (15, 7))
        ico("▶", lambda: self.seek(self.pos + 5), "Avançar 5s  (→)")
        ico("⏩", lambda: self.seek(self.pos + 10), "Avançar 10s")
        ico("⏭", lambda: self.seek(self.out), "Ir para o fim marcado")

        self.time_lbl = tk.Label(mid, text="0:00 / 0:00", bg=PANEL2, fg=TX, font=self.fm)
        self.time_lbl.pack(side="left", padx=14)

        b = Flat(mid, "[ Início", self.mark_in, font=self.fs)
        b.pack(side="left", padx=(6, 3)); Tooltip(b, "Marca o início do trecho aqui  (i)")
        b = Flat(mid, "Fim ]", self.mark_out, font=self.fs)
        b.pack(side="left", padx=3); Tooltip(b, "Marca o fim do trecho aqui  (o)")

        # velocidade e volume ficam a esquerda, fora do grupo de transporte
        left = tk.Frame(ct, bg=PANEL2); left.place(x=8, rely=.5, anchor="w")
        self.speed = tk.StringVar(value="1")
        sp = tk.OptionMenu(left, self.speed, "0.25", "0.5", "1", "1.5", "2", "4",
                           command=lambda v: self.mpv.command("speed", v))
        sp.config(bg=PANEL2, fg=TX, font=self.fs, bd=0, highlightthickness=0,
                  activebackground="#2c3744", width=4, indicatoron=0)
        sp["menu"].config(bg=PANEL2, fg=TX, font=self.fs)
        sp.pack(side="left")
        Tooltip(sp, "Velocidade de reprodução")

        self.vol = tk.Scale(left, from_=0, to=100, orient="horizontal", length=90,
                            bg=PANEL2, fg=TX2, troughcolor="#232c37", bd=0,
                            highlightthickness=0, sliderrelief="flat",
                            showvalue=False, font=self.fs,
                            command=lambda v: self.mpv.command("volume", v))
        self.vol.set(100)
        self.vol.pack(side="left", padx=(10, 0))
        Tooltip(self.vol, "Volume")

        right = tk.Frame(ct, bg=PANEL2); right.place(relx=1, x=-8, rely=.5, anchor="e")
        b = Flat(right, "⛶", self.fullscreen, pad=(11, 7), font=self.f)
        b.pack(side="right"); Tooltip(b, "Tela cheia  (f) — Esc para sair")

    def _timeline(self, parent):
        """Regua de tempo, faixa de quadros e forma de onda, no mesmo eixo."""
        wrap = tk.Frame(parent, bg=BG)
        wrap.pack(fill="x", padx=20, pady=(10, 0))

        self.ruler = tk.Canvas(wrap, bg=BG, height=RULER_H, highlightthickness=0)
        self.ruler.pack(fill="x")

        self.tl = tk.Canvas(wrap, bg="#0a0d11", height=TILE_H + 2,
                            highlightthickness=1, highlightbackground=LINE,
                            cursor="hand2")
        self.tl.pack(fill="x")

        self.wave = tk.Canvas(wrap, bg="#0a0d11", height=WAVE_H,
                              highlightthickness=1, highlightbackground=LINE,
                              cursor="hand2")
        self.wave.pack(fill="x", pady=(2, 0))

        for cv in (self.tl, self.wave):
            cv.bind("<Button-1>", self._tl_down)
            cv.bind("<B1-Motion>", self._tl_drag)
            cv.bind("<ButtonRelease-1>", self._tl_up)
            cv.bind("<MouseWheel>", self._tl_wheel)
            cv.bind("<Button-2>", self._pan_start)
            cv.bind("<B2-Motion>", self._pan_move)
        self.tl.bind("<Configure>", self._tl_resize)
        self.wave.bind("<Configure>", lambda e: self._draw_wave())
        self.ruler.bind("<Configure>", lambda e: self._draw_ruler())

        bar = tk.Frame(wrap, bg=BG)
        bar.pack(fill="x", pady=(6, 0))
        self.t0_lbl = tk.Label(bar, text="0:00", bg=BG, fg=TX3, font=self.fs)
        self.t0_lbl.pack(side="left")
        self.t1_lbl = tk.Label(bar, text="", bg=BG, fg=TX3, font=self.fs)
        self.t1_lbl.pack(side="right")

        zf = tk.Frame(bar, bg=BG)
        zf.pack()
        b = Flat(zf, "\u2212", lambda: self.zoom_by(1 / 1.8), pad=(11, 3), font=self.f)
        b.pack(side="left")
        Tooltip(b, "Afastar  (roda do mouse para baixo)")
        self.zoom_lbl = tk.Label(zf, text="1x", bg=BG, fg=TX2, font=self.fm, width=7)
        self.zoom_lbl.pack(side="left", padx=6)
        b = Flat(zf, "+", lambda: self.zoom_by(1.8), pad=(11, 3), font=self.f)
        b.pack(side="left")
        Tooltip(b, "Aproximar  (roda do mouse para cima)")
        b = Flat(zf, "tudo", self.zoom_fit, pad=(10, 3), font=self.fs)
        b.pack(side="left", padx=(8, 0))
        Tooltip(b, "Enquadra a grava\u00e7\u00e3o inteira")
        b = Flat(zf, "trecho", self.zoom_selection, pad=(10, 3), font=self.fs)
        b.pack(side="left", padx=4)
        Tooltip(b, "Enquadra o trecho marcado")

        tk.Label(parent,
                 text="Clique para pular \u00b7 arraste para marcar \u00b7 roda do mouse "
                      "aproxima \u00b7 bot\u00e3o do meio arrasta a vista \u00b7 "
                      "espa\u00e7o play \u00b7 i / o marcam in\u00edcio e fim",
                 bg=BG, fg=TX3, font=self.fs, anchor="w").pack(fill="x", padx=20,
                                                               pady=(6, 8))

    # ------------------------------------------------------------ zoom e vista
    def view_span(self) -> float:
        return (self.cur["seconds"] / self.zoom) if self.cur else 1.0

    def _clamp_view(self):
        total = self.cur["seconds"] if self.cur else 1.0
        self.view0 = max(0.0, min(self.view0, total - self.view_span()))

    def t_to_x(self, t: float, w: int) -> float:
        return (t - self.view0) / self.view_span() * w

    def x_to_t(self, x: float, w: int) -> float:
        return self.view0 + (x / max(1, w)) * self.view_span()

    def zoom_by(self, fator: float, centro=None):
        """Aproxima mantendo fixo o instante sob o cursor."""
        if not self.cur:
            return
        span = self.view_span()
        if centro is None:
            centro = (self.pos if self.view0 <= self.pos <= self.view0 + span
                      else self.view0 + span / 2)
        # piso do zoom mostra 1s de gravacao; teto e a gravacao inteira
        self.zoom = max(1.0, min(self.cur["seconds"], self.zoom * fator))
        self.view0 = centro - (centro - self.view0) * (self.view_span() / span)
        self._clamp_view()
        self._redraw_all()

    def zoom_fit(self):
        self.zoom, self.view0 = 1.0, 0.0
        self._redraw_all()

    def zoom_selection(self):
        if not self.cur or self.out - self.inp < 0.5:
            return
        dur = self.out - self.inp
        folga = dur * 0.15
        self.zoom = max(1.0, self.cur["seconds"] / (dur + folga * 2))
        self.view0 = self.inp - folga
        self._clamp_view()
        self._redraw_all()

    def _tl_wheel(self, e):
        w = max(1, e.widget.winfo_width())
        self.zoom_by(1.25 if e.delta > 0 else 1 / 1.25, self.x_to_t(e.x, w))
        return "break"

    def _pan_start(self, e):
        self._pan = (e.x, self.view0)

    def _pan_move(self, e):
        if not getattr(self, "_pan", None) or not self.cur:
            return
        x0, v0 = self._pan
        w = max(1, e.widget.winfo_width())
        self.view0 = v0 - (e.x - x0) / w * self.view_span()
        self._clamp_view()
        self._redraw_all()

    def _redraw_all(self):
        self._tl_resize()
        self._draw_ruler()
        self._draw_wave()
        if self.cur:
            self.zoom_lbl.config(text=(f"{self.zoom:.0f}x" if self.zoom >= 2
                                       else "1x"))
            self.t0_lbl.config(text=timecode(self.view0, False))
            self.t1_lbl.config(text=timecode(self.view0 + self.view_span(), False))

    # ------------------------------------------------------------ desenho
    def _draw_ruler(self):
        c = self.ruler
        c.delete("all")
        if not self.cur:
            return
        w = max(1, c.winfo_width())
        span = self.view_span()
        # passo redondo mais proximo de uma marca a cada ~110px de tela
        alvo = span * 110 / w
        passo = next((p for p in RULER_STEPS if p >= alvo), RULER_STEPS[-1])
        t = int(self.view0 // passo) * passo
        while t <= self.view0 + span:
            x = self.t_to_x(t, w)
            if -40 <= x <= w + 40:
                c.create_line(x, RULER_H - 5, x, RULER_H, fill=LINE2)
                c.create_text(x + 3, 0, text=timecode(t, False), anchor="nw",
                              fill=TX3, font=self.fs)
            t += passo

    def _draw_wave(self):
        c = self.wave
        c.delete("all")
        if not self.cur:
            return
        w = max(1, c.winfo_width())
        mid = WAVE_H / 2
        pk = self.waves.get(self.cur["id"])
        if not pk:
            c.create_text(w / 2, mid, text="analisando o \u00e1udio\u2026",
                          fill=TX3, font=self.fs)
            return
        cols = waveform.column_peaks(pk, self.view0, self.view0 + self.view_span(), w)
        sel0, sel1 = self.t_to_x(self.inp, w), self.t_to_x(self.out, w)
        for x, v in enumerate(cols):
            if v <= 0:
                continue
            alt = max(1.0, v / 255 * (mid - 2))
            cor = AC if sel0 <= x <= sel1 else "#3d4d61"
            c.create_line(x, mid - alt, x, mid + alt, fill=cor)
        c.create_line(0, mid, w, mid, fill="#222a34")
        self._overlay(c, WAVE_H, marcadores=False)

    def _overlay(self, c, h, marcadores=True):
        """Selecao, marcadores e cursor de leitura - iguais nas duas faixas."""
        w = max(1, c.winfo_width())
        x0, x1 = self.t_to_x(self.inp, w), self.t_to_x(self.out, w)
        if x1 > 0 and x0 < w:
            c.create_rectangle(max(x0, -2), 0, min(x1, w + 2), 3, fill=AC, outline="")
            c.create_rectangle(max(x0, -2), h - 3, min(x1, w + 2), h, fill=AC,
                               outline="")
            c.create_rectangle(x0, 0, x1, h, outline=AC, width=1)
            for hx in (x0, x1):
                if -6 <= hx <= w + 6:
                    c.create_rectangle(hx - 4, h / 2 - 12, hx + 4, h / 2 + 12,
                                       fill=AC, outline="#0a0d11")
                    c.create_line(hx, h / 2 - 6, hx, h / 2 + 6, fill="#0a0d11")
        if marcadores:
            for mk in self.cur["markers"]:
                x = self.t_to_x(mk["t"], w)
                if 0 <= x <= w:
                    c.create_line(x, 0, x, 10, fill=WARN, width=2)
        x = self.t_to_x(self.pos, w)
        if -2 <= x <= w + 2:
            c.create_line(x, 0, x, h, fill="white", width=2)
            c.create_polygon(x - 5, 0, x + 5, 0, x, 7, fill="white", outline="")

    def _fit_video(self):
        """Encaixa o quadro do video no espaco livre mantendo a proporcao."""
        w = self.video_wrap.winfo_width()
        h = self.video_wrap.winfo_height()
        if w < 20 or h < 20:
            return
        ar = 16 / 9
        if self.cur and self.cur.get("width") and self.cur.get("height"):
            ar = self.cur["width"] / self.cur["height"]
        vw, vh = (int(h * ar), h) if w / h > ar else (w, int(w / ar))
        self.video.place_configure(x=(w - vw) // 2, y=(h - vh) // 2,
                                   width=vw, height=vh,
                                   relx=0, rely=0, relwidth=0, relheight=0)

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
            Tooltip(e, {"e_in": "Onde o trecho começa (mm:ss ou h:mm:ss)",
                        "e_out": "Onde o trecho termina",
                        "e_dur": "Duração do trecho — calculada"}[attr])
            setattr(self, attr, e)

        f = tk.Frame(row, bg=BG); f.pack(side="left", fill="x", expand=True, padx=(0, 12))
        hdr = tk.Frame(f, bg=BG); hdr.pack(fill="x")
        tk.Label(hdr, text="NOME DO ARQUIVO", bg=BG, fg=TX3, font=self.fs,
                 anchor="w").pack(side="left")
        # O sufixo de trecho era obrigatorio; agora e escolha, e mostra timecode
        # em vez de segundos crus.
        self.suffix_on = tk.BooleanVar(value=CFG.settings.get("name_suffix", True))
        chk = tk.Checkbutton(hdr, text="incluir trecho no nome",
                             variable=self.suffix_on, command=self._name_changed,
                             bg=BG, fg=TX3, font=self.fs, bd=0, highlightthickness=0,
                             activebackground=BG, activeforeground=TX,
                             selectcolor=PANEL2, cursor="hand2")
        chk.pack(side="right")
        Tooltip(chk, "Acrescenta o trecho ao nome, como [12m30s-14m00s].\n"
                     "Desligado, o arquivo fica só com o nome que você escreveu.")
        self.e_name = tk.Entry(f, bg=PANEL2, fg=TX, font=self.f, bd=0,
                               insertbackground=TX)
        self.e_name.pack(fill="x", ipady=5)
        self.e_name.bind("<KeyRelease>", lambda e: self._name_changed())
        self.name_preview = tk.Label(f, text="", bg=BG, fg=TX3, font=self.fs,
                                     anchor="w")
        self.name_preview.pack(fill="x", pady=(3, 0))
        Tooltip(self.e_name, "Nome do arquivo exportado.\nVai para: " + str(CFG.output))

        f = tk.Frame(row, bg=BG); f.pack(side="left")
        tk.Label(f, text="QUALIDADE", bg=BG, fg=TX3, font=self.fs, anchor="w").pack(fill="x")
        self.quality = tk.StringVar(value="19")
        q = tk.OptionMenu(f, self.quality, "15", "19", "23")
        Tooltip(f, "Qualidade do vídeo no preset Entrega (CQ do NVENC).\n"
                   "15 = máxima, arquivo maior · 19 = alta (padrão)\n"
                   "23 = menor arquivo, perde detalhe em cena rápida.\n"
                   "Ignorado em Original e Edição.")
        q.config(bg=PANEL2, fg=TX, font=self.fs, bd=0, highlightthickness=0,
                 activebackground="#2c3744", width=5)
        q["menu"].config(bg=PANEL2, fg=TX, font=self.fs)
        q.pack(fill="x")

        # grid com colunas de peso igual: com pack os cards mudavam de tamanho
        # conforme o texto de cada um. Altura fixa e texto curto de duas linhas
        # mantem todos iguais.
        pr = tk.Frame(mn, bg=BG); pr.pack(fill="x", padx=16, pady=(14, 0))
        self.preset_btns = {}
        cards = list(PRESETS.items()) + [(CUSTOM, CUSTOM_META)]
        for col, (key, meta) in enumerate(cards):
            pr.columnconfigure(col, weight=1, uniform="preset")
            b = tk.Frame(pr, bg=PANEL2, highlightthickness=1, height=78, width=10,
                         highlightbackground=AC if key == self.preset else LINE,
                         cursor="hand2")
            b.grid(row=0, column=col, sticky="nsew", padx=4)
            b.grid_propagate(False)
            head = tk.Frame(b, bg=PANEL2, width=1)   # largura vem do grid, nao do texto
            head.pack(fill="x", padx=12, pady=(9, 0))
            head.pack_propagate(False)
            head.config(height=20)
            tk.Label(head, text=meta["label"], bg=PANEL2, fg=TX, font=self.fb,
                     anchor="w").pack(side="left")
            if key == CUSTOM:
                tk.Label(head, text="⚙", bg=PANEL2, fg=TX3,
                         font=self.f).pack(side="right")
            tk.Label(b, text=meta["short"], bg=PANEL2, fg=TX2, font=self.fs,
                     anchor="nw", justify="left").pack(fill="both", expand=True,
                                                       padx=12, pady=(3, 9))
            for w in (b, head, *head.winfo_children(), *b.winfo_children()):
                w.bind("<Button-1>", lambda e, k=key: self.set_preset(k))
            Tooltip(b, meta["hint"])
            self.preset_btns[key] = b

        act = tk.Frame(mn, bg=BG); act.pack(fill="x", padx=20, pady=14)
        self.exp_btn = Flat(act, "Exportar trecho", self.export, bg=AC, fg="white",
                            pad=(20, 9), font=self.fb)
        self.exp_btn.pack(side="right")
        Flat(act, "Fechar player", self.close_player, font=self.fs).pack(side="right", padx=8)

    def _jobs_bar(self):
        self.jobs_frame = tk.Frame(self.root, bg=PANEL)
        inner = tk.Frame(self.jobs_frame, bg=PANEL)
        inner.pack(fill="x", padx=20, pady=7)
        self.jobs_lbl = tk.Label(inner, text="", bg=PANEL, fg=TX2, font=self.fs,
                                 anchor="w")
        self.jobs_lbl.pack(side="left", fill="x", expand=True)

        self.jobs_bar = tk.Canvas(inner, width=150, height=5, bg="#232c37",
                                  highlightthickness=0)
        self.jobs_bar.pack(side="left", padx=12)

        self.jobs_cancel = Flat(inner, "Cancelar", self._cancel_job, font=self.fs)
        Tooltip(self.jobs_cancel,
                "Interrompe a exportação e apaga o arquivo pela metade")
        self.jobs_close = Flat(inner, "✕", self._clear_jobs, pad=(9, 5), font=self.fs)
        Tooltip(self.jobs_close, "Limpa a lista")
        self._active_job = None

    def _cancel_job(self):
        if self._active_job:
            JOBS.cancel(self._active_job)

    def _clear_jobs(self):
        JOBS.clear_finished()
        if not JOBS.snapshot():
            self.jobs_frame.pack_forget()

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
        self.zoom, self.view0 = 1.0, 0.0
        self._sync()
        self._fit_video()
        self._redraw_all()
        self._load_wave(s)
        self.open_player(0)

    def open_player(self, t):
        if not self.mpv.available:
            self.video_hint.config(text="libmpv não encontrada — instale o mpv.net")
            return
        sid = self.cur["id"]
        has_audio = bool(chunk_list(CFG.video_dir / sid, 1))
        self.video.update_idletasks()
        if self.mpv.h is None and self.mpv.wid is None:
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

    def fullscreen(self):
        """Reparenta o mpv numa janela sem borda e devolve ao sair.

        Trocar o "wid" de uma instancia viva nao e suportado, entao o player e
        recriado na posicao atual - o seek e instantaneo, o corte e imperceptivel.
        """
        if self._fs:
            pos = self.pos
            self._fs.destroy(); self._fs = None
            self.mpv.destroy()
            self.mpv = Mpv(CFG.libmpv, wid=self.video.winfo_id())
            self.open_player(pos)
            return
        if not self.cur or self.mpv.h is None:
            return
        pos = self.pos
        self._fs = tw = tk.Toplevel(self.root)
        tw.attributes("-fullscreen", True)
        tw.configure(bg="black")
        holder = tk.Frame(tw, bg="black")
        holder.pack(fill="both", expand=True)
        tw.update_idletasks()
        tw.bind("<Escape>", lambda e: self.fullscreen())
        tw.bind("f", lambda e: self.fullscreen())
        tw.bind("<space>", lambda e: self.toggle())
        tw.protocol("WM_DELETE_WINDOW", self.fullscreen)
        self.mpv.destroy()
        self.mpv = Mpv(CFG.libmpv, wid=holder.winfo_id())
        self.open_player(pos)

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
        self._follow_playhead()
        self._repaint()

    def mark_in(self):
        self.inp = self.pos
        if self.out <= self.inp:
            self.out = min(self.cur["seconds"], self.inp + 30)
        self._sync(); self._repaint()

    def mark_out(self):
        self.out = max(self.inp + 1, self.pos)
        self._sync(); self._repaint()

    def set_preset(self, key):
        self.preset = key
        for k, b in self.preset_btns.items():
            b.config(highlightbackground=AC if k == key else LINE)
        self._name_changed()
        if key == CUSTOM:
            self.open_custom()

    def open_custom(self):
        """Janela de configuracao do preset Personalizado.

        Instancia unica: clicar de novo traz a que ja esta aberta para a frente,
        em vez de empilhar copias.
        """
        if self.dlg and self.dlg.winfo_exists():
            self.dlg.lift(); self.dlg.focus_force()
            return
        if not self.cur:
            return
        self.dlg = CustomDialog(self, self.custom_opts)
        self.root.wait_window(self.dlg)
        if self.dlg.result:
            self.custom_opts = self.dlg.result
            CFG.settings['custom_opts'] = self.custom_opts
            from steamclipper.config import save_settings
            save_settings(CFG.settings)
        self.dlg = None
        self._update_custom_card()

    def _update_custom_card(self):
        """Mostra no card o que foi configurado, em vez do texto generico."""
        o = self.custom_opts
        card = self.preset_btns.get(CUSTOM)
        try:
            if not card or not card.winfo_exists():
                return
            lbl = [w for w in card.winfo_children() if isinstance(w, tk.Label)]
        except tk.TclError:      # janela fechando enquanto o modal retorna
            return
        if not lbl:
            return
        if o:
            res = SCALES.get(int(o.get("scale") or 0), "Original")
            cod = {"h264": "H.264", "hevc": "H.265", "copy": "Cópia",
                   "dnxhr": "DNxHR"}.get(o.get("codec"), o.get("codec", ""))
            extra = (f"{int(o['bitrate'])} kbps" if o.get("bitrate")
                     else QUALITY_LEVELS.get(int(o.get("quality", 19)), "").split(" (")[0])
            aud = {"none": " · sem áudio",
                   "normalize": " · áudio nivelado"}.get(o.get("audio"), "")
            lbl[-1].config(text=f"{cod} · {res} · {o.get('container','mp4').upper()}\n"
                                f"{extra}{aud}")
        else:
            lbl[-1].config(text=CUSTOM_META["short"])

    def reload(self):
        self.sessions = scan_sessions(CFG)
        self._fill_list()

    def reveal(self):
        CFG.output.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["explorer", str(CFG.output)])

    def export(self):
        """Exporta com o preset selecionado. Este botao sempre renderiza."""
        if not self.cur:
            return
        dur = max(0.5, self.out - self.inp)
        name = self.e_name.get().strip() or self.cur["game"]

        if self.preset == CUSTOM:
            if not self.custom_opts:          # nunca configurou: abre e volta
                self.open_custom()
                if not self.custom_opts:
                    return
            opts = dict(self.custom_opts)
            base = "deliver"
        else:
            opts = preset_opts(self.preset)
            base = self.preset
        opts["outdir"] = str(CFG.output)
        opts["suffix"] = bool(self.suffix_on.get())

        jid = JOBS.submit(self.cur["id"], base, self.inp, dur, name, opts)
        JOBS._upd(jid, start=self.inp)       # o painel mostra o trecho exportado
        if self.render_dlg and self.render_dlg.winfo_exists():
            self.render_dlg.destroy()
        self.render_dlg = RenderDialog(self, jid)

    def quit(self):
        self.mpv.destroy()
        self.root.destroy()

    # ------------------------------------------------------- timeline
    def _tl_pos(self, e):
        w = max(1, e.widget.winfo_width())
        t = self.x_to_t(e.x, w)
        return max(0.0, min(self.cur["seconds"], t))

    def _tl_down(self, e):
        if not self.cur:
            return
        self._down = self._tl_pos(e); self._moved = False

    def _tl_drag(self, e):
        if not self.cur or not hasattr(self, "_down"):
            return
        t = self._tl_pos(e)
        if abs(t - self._down) > self.view_span() * 0.004:
            self._moved = True
        if self._moved:
            self.inp, self.out = min(self._down, t), max(self._down, t)
            self._sync(); self._draw_tl(); self._draw_wave()

    def _follow_playhead(self):
        """Rola a vista para acompanhar a reproducao, sem sequestra-la.

        So age quando o cursor sai pouco alem da borda durante a reproducao - o
        avanco natural do video. Se ele esta longe, quem mandou na vista foi o
        usuario (deu zoom num trecho, arrastou a vista) e a vista fica onde esta.
        """
        if not self.cur or self.zoom <= 1.0 or not self.playing:
            return
        span = self.view_span()
        fim = self.view0 + span
        if self.view0 <= self.pos <= fim:
            return
        if not (fim < self.pos <= fim + span * 0.5):
            return                      # longe demais: foi navegacao, nao reproducao
        self.view0 = self.pos - span * 0.25
        self._clamp_view()
        self._tl_resize()
        self._draw_ruler()
        self.t0_lbl.config(text=timecode(self.view0, False))
        self.t1_lbl.config(text=timecode(self.view0 + span, False))

    def _tl_up(self, e):
        if not self.cur or not hasattr(self, "_down"):
            return
        if not self._moved:
            self.seek(self._tl_pos(e))
        del self._down

    def _tl_resize(self, _e=None):
        """Redesenha ao redimensionar, com folga para nao gerar a cada pixel."""
        if self._tl_job:
            self.root.after_cancel(self._tl_job)
        self._tl_job = self.root.after(180, lambda: (self._load_tiles(), self._draw_tl()))

    def _load_wave(self, s):
        """Le os picos do cache ou dispara a analise; a faixa se redesenha sozinha."""
        sid = s["id"]
        if sid in self.waves:
            return
        pk = waveform.load(CFG, sid)
        if pk:
            self.waves[sid] = pk
            self._draw_wave()
            return

        def pronto(dados):
            def aplicar():
                self.waves[sid] = dados
                if self.cur and self.cur["id"] == sid:
                    self._draw_wave()
            try:
                self.root.after(0, aplicar)
            except tk.TclError:
                pass
        waveform.extract(CFG, sid, PORT, on_done=pronto)

    def _load_tiles(self):
        """Gera os quadros da linha do tempo no tamanho exato em que serao usados.

        A versao anterior montava uma faixa unica e a esticava; como a largura era
        decidida antes do canvas existir, winfo_width() devolvia 1, caia no minimo
        de 400px e a faixa cobria so um terco da timeline. Aqui cada quadro tem
        largura fixa e e desenhado na posicao dele, entao a faixa sempre fecha.
        """
        s = self.cur
        if not s:
            return
        w = max(1, self.tl.winfo_width())
        if w < 50:
            return
        n = max(1, -(-w // TILE_W))            # ceil
        span = self.view_span()
        want = [self.view0 + (i + 0.5) / n * span for i in range(n)]
        self._tile_times = want

        def work():
            for i, t in enumerate(want):
                key = (s["id"], int(t))
                if key in self.tiles:
                    continue
                try:
                    p = thumb(CFG, s["id"], t, width=TILE_W, fmt="png")
                except Exception:                              # noqa: BLE001
                    continue
                def apply(path=p, k=key, idx=i):
                    if not self.cur or self.cur["id"] != k[0]:
                        return
                    try:
                        self.tiles[k] = tk.PhotoImage(file=str(path))
                    except tk.TclError:
                        return
                    if idx % 4 == 0 or idx == len(want) - 1:
                        self._draw_tl()
                self.root.after(0, apply)
        threading.Thread(target=work, daemon=True).start()

    def _repaint(self):
        """Redesenha as duas faixas sem regerar quadros - so a camada de cima."""
        self._draw_tl()
        self._draw_wave()

    def _draw_tl(self):
        c = self.tl
        c.delete("all")
        if not self.cur:
            return
        w, h = max(1, c.winfo_width()), TILE_H
        for i, t in enumerate(getattr(self, "_tile_times", [])):
            img = self.tiles.get((self.cur["id"], int(t)))
            x = i * TILE_W
            if img:
                c.create_image(x, 0, image=img, anchor="nw")
            else:
                c.create_rectangle(x, 0, x + TILE_W, h,
                                   fill="#12171d" if i % 2 else "#161c23", outline="")
        self._overlay(c, h)

    def _name_changed(self):
        """Mostra o nome final e guarda a preferencia de sufixo."""
        if not self.cur:
            return
        dur = max(MIN_DURATION, self.out - self.inp)
        base_preset = self.preset if self.preset != CUSTOM else "deliver"
        ext = preset_opts(base_preset)["container"]
        if self.preset == CUSTOM and self.custom_opts:
            ext = self.custom_opts.get("container", ext)
        nome = output_name(self.e_name.get(), self.inp, dur, ext,
                           with_suffix=self.suffix_on.get(),
                           fallback=self.cur["game"])
        opts = (self.custom_opts if self.preset == CUSTOM and self.custom_opts
                else preset_opts(self.preset))
        self.name_preview.config(text=f"{nome}   \u00b7   {format_estimate(opts, dur)}")
        if CFG.settings.get("name_suffix") != self.suffix_on.get():
            CFG.settings["name_suffix"] = self.suffix_on.get()
            from steamclipper.config import save_settings
            save_settings(CFG.settings)

    def _sync(self):
        self.e_in.delete(0, "end"); self.e_in.insert(0, fmt(self.inp))
        self.e_out.delete(0, "end"); self.e_out.insert(0, fmt(self.out))
        self.e_dur.config(state="normal")
        self.e_dur.delete(0, "end"); self.e_dur.insert(0, fmt(self.out - self.inp))
        self.e_dur.config(state="disabled")
        self._name_changed()

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
    def _draw_progress(self, pct, color):
        c = self.jobs_bar
        c.delete("all")
        c.create_rectangle(0, 0, 150 * max(0, min(100, pct)) / 100, 5,
                           fill=color, outline="")

    def _tick(self):
        st = self.mpv.state()
        if st.get("open"):
            self.pos = st["pos"]
            self.playing = not st["paused"]
            self.play_btn.config(text="▶" if st["paused"] else "⏸")
            if self.cur:
                self.time_lbl.config(text=f"{fmt(self.pos)} / {fmt(self.cur['seconds'])}")
            self._follow_playhead()
            self._repaint()

        jobs = JOBS.snapshot()
        if jobs:
            act = [j for j in jobs.values()
                   if j["status"] not in ("pronto", "erro", "cancelado")]
            self._active_job = act[0]["id"] if act else None
            if act:
                j = act[0]
                est = j.get("estimate_mb")
                extra = f" · ~{est:.0f} MB previstos" if est else ""
                self.jobs_lbl.config(text=f"{j['label']} · {j['status']} "
                                          f"{j.get('pct', 0)}%{extra}", fg=AC)
                self.jobs_cancel.pack(side="left", padx=(0, 6))
                self.jobs_close.pack_forget()
                self._draw_progress(j.get("pct", 0), AC)
            else:
                last = list(jobs.values())[-1]
                st = last["status"]
                if st == "pronto":
                    self.jobs_lbl.config(
                        text=f"{last['label']} · pronto · "
                             f"{last.get('size', 0) / 1048576:.1f} MB  →  "
                             f"{last.get('out', CFG.output)}", fg=OK)
                    self._draw_progress(100, OK)
                elif st == "cancelado":
                    self.jobs_lbl.config(text=f"{last['label']} · cancelado · "
                                              f"arquivo parcial removido", fg=WARN)
                    self._draw_progress(0, WARN)
                else:
                    self.jobs_lbl.config(text=f"{last['label']} · erro: "
                                              f"{last.get('error', '')}", fg=ERR)
                    self._draw_progress(100, ERR)
                self.jobs_cancel.pack_forget()
                self.jobs_close.pack(side="left")
            self.jobs_frame.pack(fill="x", side="bottom")
        else:
            self.jobs_frame.pack_forget()
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
