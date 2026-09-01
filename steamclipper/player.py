# -*- coding: utf-8 -*-
"""Player mpv via libmpv.

O mpv.net instalado nao e o mpv.exe: e um frontend .NET sobre libmpv-2.dll. Essa
DLL e o mpv de verdade e da para conduzi-la por ctypes - entao o app controla um
player nativo em vez de transcodificar. Ele le o HEVC 1440p original com
decodificacao por hardware (d3d11va) e o primeiro quadro aparece em ~0,4s em
qualquer ponto da gravacao.

Duas formas de uso:
  janela propria  -> Mpv().open(url)              (versao Browser)
  embutido        -> Mpv(wid=hwnd).open(url)      (versao Desktop)
"""

from __future__ import annotations

import threading
import time
from pathlib import Path


class Mpv:
    def __init__(self, dll: Path | None, wid: int | None = None,
                 extra: dict[str, str] | None = None):
        self.dll = dll
        self.wid = wid
        self.extra = extra or {}
        self.lib = None
        self.ctypes = None
        self.h = None
        self.title = None
        self.lock = threading.RLock()

    @property
    def available(self) -> bool:
        return bool(self.dll and Path(self.dll).is_file())

    # ------------------------------------------------------------- ctypes
    def _load(self):
        if self.lib:
            return
        import ctypes
        import os
        os.add_dll_directory(str(Path(self.dll).parent))
        lib = ctypes.CDLL(str(self.dll))
        lib.mpv_create.restype = ctypes.c_void_p
        for name, argt, rest in [
            ("mpv_initialize", [ctypes.c_void_p], ctypes.c_int),
            ("mpv_set_option_string",
             [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p], ctypes.c_int),
            ("mpv_get_property_string",
             [ctypes.c_void_p, ctypes.c_char_p], ctypes.c_void_p),
            ("mpv_free", [ctypes.c_void_p], None),
            ("mpv_terminate_destroy", [ctypes.c_void_p], None),
        ]:
            fn = getattr(lib, name)
            fn.argtypes, fn.restype = argt, rest
        lib.mpv_command.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_char_p)]
        self.ctypes, self.lib = ctypes, lib

    def _cmd(self, *args) -> int:
        c = self.ctypes
        arr = (c.c_char_p * (len(args) + 1))(*[str(a).encode() for a in args], None)
        return self.lib.mpv_command(self.h, arr)

    def _get(self, name):
        p = self.lib.mpv_get_property_string(self.h, name.encode())
        if not p:
            return None
        s = self.ctypes.string_at(p).decode("utf-8", "replace")
        self.lib.mpv_free(p)
        return s

    def _spawn(self):
        self._load()
        h = self.lib.mpv_create()
        opts = {
            "vo": "gpu", "hwdec": "auto-safe", "keep-open": "yes",
            "force-window": "yes", "title": "SteamClipper",
            "input-default-bindings": "yes", "input-vo-keyboard": "yes",
        }
        if self.wid is not None:
            opts.update({"wid": str(self.wid), "osc": "no", "input-vo-keyboard": "no"})
        else:
            opts.update({"osc": "yes", "geometry": "62%"})
        opts.update(self.extra)
        for k, v in opts.items():
            self.lib.mpv_set_option_string(h, k.encode(), str(v).encode())
        if self.lib.mpv_initialize(h) != 0:
            self.lib.mpv_terminate_destroy(h)
            raise RuntimeError("mpv_initialize falhou")
        self.h = h

    # -------------------------------------------------------------- API
    def open(self, url: str, t: float = 0.0, audio_url: str | None = None,
             title: str | None = None):
        with self.lock:
            if self.h is None:
                self._spawn()
            self._cmd("loadfile", url)
            self.title = title
            # audio-add so funciona DEPOIS do arquivo carregar; passado como opcao
            # de inicializacao (--audio-file) a faixa e simplesmente ignorada.
            for _ in range(40):
                time.sleep(0.05)
                if self._get("duration"):
                    break
            if audio_url:
                self._cmd("audio-add", audio_url, "select")
            if t > 0:
                self._cmd("seek", f"{t:.3f}", "absolute")

    def command(self, action: str, value=None) -> dict:
        with self.lock:
            if self.h is None:
                return {"error": "player fechado"}
            try:
                if action == "seek":
                    self._cmd("seek", f"{float(value):.3f}", "absolute")
                elif action == "seek_rel":
                    self._cmd("seek", f"{float(value):.3f}", "relative")
                elif action == "pause":
                    self._cmd("set", "pause", "yes")
                elif action == "play":
                    self._cmd("set", "pause", "no")
                elif action == "toggle":
                    self._cmd("cycle", "pause")
                elif action == "frame_step":
                    self._cmd("frame-step" if float(value or 1) > 0 else "frame-back-step")
                elif action == "speed":
                    self._cmd("set", "speed", str(value))
                elif action == "volume":
                    self._cmd("set", "volume", str(value))
                elif action == "close":
                    self.destroy()
                else:
                    return {"error": f"acao desconhecida: {action}"}
            except Exception as e:                             # noqa: BLE001
                return {"error": str(e)}
            return {"ok": True}

    def state(self) -> dict:
        with self.lock:
            if self.h is None:
                return {"open": False}
            try:
                dur = self._get("duration")
                return {
                    "open": True,
                    "pos": float(self._get("time-pos") or 0),
                    "duration": float(dur) if dur else 0,
                    "paused": self._get("pause") == "yes",
                    "speed": float(self._get("speed") or 1),
                    "title": self.title,
                }
            except Exception:                                  # noqa: BLE001
                return {"open": False}

    def destroy(self):
        with self.lock:
            if self.h is not None:
                try:
                    self.lib.mpv_terminate_destroy(self.h)
                except Exception:                              # noqa: BLE001
                    pass
                self.h = None
                self.title = None
