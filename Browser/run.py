#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SteamClipper - versao navegador.

Serve o painel em http://127.0.0.1:8777 e controla um mpv em janela propria.
Uma pagina web nao pode hospedar uma janela nativa, entao aqui o painel e o
controle remoto do player; para o video dentro da interface, use a versao Desktop.

Rotas:
  GET  /                    o painel
  GET  /api/sessions        gravacoes disponiveis
  GET  /api/thumb           1 quadro JPEG
  GET  /api/strip           faixa de quadros da linha do tempo
  GET  /api/stream          fMP4 virtual com Range (e o que o mpv consome)
  POST /api/mpv/open        abre a gravacao no player
  POST /api/mpv/cmd         play/pause/seek/velocidade
  GET  /api/mpv/state       posicao atual
  POST /api/export          exporta um trecho
  GET  /api/jobs            progresso das exportacoes
  POST /api/reveal          abre a pasta de saida
  POST /api/shutdown        encerra o servidor e o player
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from steamclipper import (PRESETS, Config, Jobs, Mpv, scan_sessions, strip,
                          thumb, virtual)  # noqa: E402
from steamclipper.steam import chunk_list                                        # noqa: E402

HERE = Path(__file__).resolve().parent
CFG = Config()
JOBS = Jobs(CFG)
PLAYER = Mpv(CFG.libmpv)
PORT = Config.PORT

CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css",
                 ".js": "text/javascript", ".png": "image/png",
                 ".jpg": "image/jpeg", ".svg": "image/svg+xml",
                 ".ico": "image/x-icon"}


def stream_url(sid: str, stream: int = 0) -> str:
    u = f"http://127.0.0.1:{PORT}/api/stream?session={quote(sid)}"
    return u + "&stream=1" if stream else u


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # ------------------------------------------------------------- helpers
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), code and
                   "application/json; charset=utf-8")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    # --------------------------------------------------------------- GET
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, (HERE / "ui.html").read_bytes(),
                                  CONTENT_TYPES[".html"])

            if not u.path.startswith("/api/"):
                f = HERE / os.path.basename(u.path)
                if f.name and f.is_file() and f.parent == HERE:
                    ct = CONTENT_TYPES.get(f.suffix.lower(), "application/octet-stream")
                    return self._send(200, f.read_bytes(), ct)

            if u.path == "/api/sessions":
                return self._json({"sessions": scan_sessions(CFG),
                                   "out_dir": str(CFG.output),
                                   "mpv": PLAYER.available,
                                   "problems": CFG.problems(),
                                   "presets": {k: {"label": v["label"],
                                                   "hint": v["hint"]}
                                               for k, v in PRESETS.items()}})

            if u.path == "/api/thumb":
                p = thumb(CFG, q["session"][0], float(q.get("t", ["0"])[0]))
                return self._send(200, p.read_bytes(), "image/jpeg",
                                  {"Cache-Control": "max-age=86400"})

            if u.path == "/api/strip":
                n = max(4, min(32, int(q.get("n", ["16"])[0])))
                p = strip(CFG, q["session"][0], n)
                return self._send(200, p.read_bytes(), "image/jpeg",
                                  {"Cache-Control": "max-age=86400"})

            if u.path == "/api/mpv/state":
                return self._json(PLAYER.state())

            if u.path == "/api/jobs":
                return self._json({"jobs": JOBS.snapshot()})

            if u.path == "/api/stream":
                return self._stream(q)

            return self._send(404, b"nao encontrado", "text/plain; charset=utf-8")
        except Exception as e:                                 # noqa: BLE001
            return self._json({"error": str(e)}, 500)

    def do_HEAD(self):
        self.do_GET()

    # -------------------------------------------------------------- POST
    def do_POST(self):
        u = urlparse(self.path)
        try:
            b = self._body()

            if u.path == "/api/mpv/open":
                if not PLAYER.available:
                    return self._json({"error": "libmpv nao encontrada "
                                                "(instale o mpv.net)"}, 400)
                sid = b["session"]
                has_audio = bool(chunk_list(CFG.video_dir / sid, 1))
                PLAYER.open(stream_url(sid), float(b.get("t", 0)),
                            stream_url(sid, 1) if has_audio else None,
                            title=b.get("name", sid))
                st = PLAYER.state()
                st["session"] = sid
                return self._json(st)

            if u.path == "/api/mpv/cmd":
                return self._json(PLAYER.command(b["action"], b.get("value")))

            if u.path == "/api/export":
                jid = JOBS.submit(b["session"], b.get("preset", "deliver"),
                                  float(b.get("start", 0)), float(b.get("duration", 0)),
                                  b.get("name", b["session"]), b.get("opts"))
                return self._json({"job": jid})

            if u.path == "/api/export/cancel":
                return self._json(JOBS.cancel(b["job"]))

            if u.path == "/api/export/clear":
                JOBS.clear_finished()
                return self._json({"ok": True})

            if u.path == "/api/output":
                try:
                    CFG.set_output(b["path"])
                    return self._json({"ok": True, "out_dir": str(CFG.output)})
                except OSError as e:
                    return self._json({"error": str(e)}, 400)

            if u.path == "/api/reveal":
                CFG.output.mkdir(parents=True, exist_ok=True)
                subprocess.Popen(["explorer", str(CFG.output)])
                return self._json({"ok": True})

            if u.path == "/api/shutdown":
                PLAYER.destroy()          # senao a janela do mpv fica orfa
                self._json({"ok": True})
                threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)),
                                 daemon=True).start()
                return

            return self._send(404, b"nao encontrado", "text/plain; charset=utf-8")
        except Exception as e:                                 # noqa: BLE001
            return self._json({"error": str(e)}, 500)

    # ------------------------------------------------- stream com Range
    def _stream(self, q):
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
                elif m.group(2):                      # sufixo: ultimos N bytes
                    start = max(0, total - int(m.group(2)))
                code = 206
        if start > end or start >= total:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{total}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.send_response(code)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        # O mpv aborta a conexao a cada seek. Sem isto o keep-alive tenta reusar
        # um socket com resposta pela metade e o ffmpeg reporta "End of file".
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            for buf in vf.read(start, end):
                self.wfile.write(buf)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass


class Server(ThreadingHTTPServer):
    # No Windows SO_REUSEADDR deixa dois processos escutarem a mesma porta, e o
    # segundo sobe calado sem receber requisicao. Desligar faz o conflito virar
    # erro visivel em vez de servidor fantasma.
    allow_reuse_address = False
    daemon_threads = True


def already_running() -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/sessions",
                                    timeout=2) as r:
            json.loads(r.read())
            return True
    except Exception:                                          # noqa: BLE001
        return False


def main():
    url = f"http://127.0.0.1:{PORT}/"
    quiet = "--no-browser" in sys.argv

    # Abrir duas vezes e o caminho normal (clicar no atalho de novo). Em vez de
    # falhar por porta ocupada, so traz o painel de volta ao navegador.
    if already_running():
        print("O SteamClipper ja esta aberto - reabrindo o painel no navegador.")
        if not quiet:
            webbrowser.open(url)
        return

    print(CFG.describe(), "\n")
    for p in CFG.problems():
        print("  aviso:", p)
    if not CFG.ok:
        print("\nSem gravacoes para mostrar.")
        sys.exit(1)

    try:
        srv = Server(("127.0.0.1", PORT), Handler)
    except OSError as e:
        print(f"Nao consegui abrir a porta {PORT}: {e}")
        print("Rode Encerrar.bat e tente de novo.")
        sys.exit(1)

    print(f"SteamClipper em {url}")
    print("Feche esta janela (ou rode Encerrar.bat) para desligar.\n")
    if not quiet:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        PLAYER.destroy()
        print("\nencerrado")


if __name__ == "__main__":
    main()
