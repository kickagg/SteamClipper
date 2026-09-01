# -*- coding: utf-8 -*-
"""Descoberta automatica de caminhos.

Nada aqui e fixo no codigo: a instalacao do Steam sai do registro, o usuario sai
da pasta userdata e a pasta das gravacoes sai do proprio localconfig.vdf. Assim o
app acompanha quando voce muda a pasta de gravacao nas opcoes do Steam, e funciona
na maquina de qualquer pessoa sem editar nada.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

APP_NAME = "SteamClipper"


def decode(b: bytes) -> str:
    """Arquivos do Steam misturam UTF-8 e CP1252 (o (TM) vem em CP1252)."""
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


def _read(p: Path) -> str:
    try:
        return decode(p.read_bytes())
    except OSError:
        return ""


def find_steam() -> Path | None:
    """Instalacao do Steam: registro primeiro, depois os locais de sempre."""
    try:
        import winreg
        for root, key in ((winreg.HKEY_CURRENT_USER, r"SOFTWARE\Valve\Steam"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam")):
            try:
                with winreg.OpenKey(root, key) as k:
                    for name in ("SteamPath", "InstallPath"):
                        try:
                            v = winreg.QueryValueEx(k, name)[0]
                        except FileNotFoundError:
                            continue
                        if v:
                            p = Path(v.replace("/", "\\"))
                            if p.is_dir():
                                return p
            except OSError:
                continue
    except ImportError:
        pass

    for guess in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
        p = Path(guess)
        if p.is_dir():
            return p
    return None


def steam_libraries(steam: Path) -> list[Path]:
    """Pastas de biblioteca (a principal + as extras do libraryfolders.vdf)."""
    libs = [steam]
    for m in re.finditer(r'"path"\s+"([^"]+)"',
                         _read(steam / "steamapps" / "libraryfolders.vdf")):
        p = Path(m.group(1).replace("\\\\", "\\"))
        if p.is_dir() and p not in libs:
            libs.append(p)
    return libs


def _localconfigs(steam: Path) -> list[tuple[Path, str]]:
    """(pasta do usuario, conteudo do localconfig) por conta, mais recente antes."""
    root = steam / "userdata"
    out = []
    if not root.is_dir():
        return out
    for d in root.iterdir():
        if not d.is_dir() or not d.name.isdigit():
            continue
        cfg = d / "config" / "localconfig.vdf"
        if cfg.is_file():
            out.append((d, _read(cfg)))
    out.sort(key=lambda t: (t[0] / "config" / "localconfig.vdf").stat().st_mtime,
             reverse=True)
    return out


def find_recordings(steam: Path | None = None) -> tuple[Path | None, Path | None]:
    """(pasta das gravacoes, pasta do usuario).

    O Steam guarda o destino escolhido em GameRecording/BackgroundRecordPath. Ler
    de la e o que faz o app seguir voce se mudar a pasta nas opcoes.
    """
    steam = steam or find_steam()
    if not steam:
        return None, None

    for userdir, txt in _localconfigs(steam):
        m = re.search(r'"BackgroundRecordPath"\s+"([^"]+)"', txt)
        if m:
            p = Path(m.group(1).replace("\\\\", "\\"))
            if (p / "video").is_dir() or p.is_dir():
                return p, userdir

    # Sem a chave no config: tenta o local padrao de cada conta.
    for userdir, _ in _localconfigs(steam):
        p = userdir / "gamerecordings"
        if p.is_dir():
            return p, userdir
    return None, None


def find_libmpv() -> Path | None:
    """libmpv-2.dll - o mpv de verdade por tras do mpv.net."""
    names = ("libmpv-2.dll", "mpv-2.dll", "libmpv.dll")
    roots = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "mpv.net",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "mpv",
        Path(os.environ.get("ProgramFiles", "")) / "mpv.net",
        Path(os.environ.get("ProgramFiles", "")) / "mpv",
        Path(os.environ.get("APPDATA", "")) / "mpv.net",
    ]
    for r in roots:
        for n in names:
            p = r / n
            if p.is_file():
                return p
    return None


def default_output() -> Path:
    """Onde os clipes exportados vao parar (respeita o ExportDirectory do Steam)."""
    steam = find_steam()
    if steam:
        for _userdir, txt in _localconfigs(steam):
            m = re.search(r'"ExportDirectory"\s+"([^"]+)"', txt)
            if m:
                p = Path(m.group(1).replace("\\\\", "\\"))
                if p.is_dir():
                    return p
    return Path.home() / "Videos" / APP_NAME


def cache_dir() -> Path:
    p = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME / "thumbs"
    p.mkdir(parents=True, exist_ok=True)
    return p


class Config:
    """Caminhos resolvidos uma vez, usados por Browser e Desktop."""

    SEGMENT_SECONDS = 3.0        # SegmentTemplate duration=3000000 / timescale=1000000
    PORT = 8777

    def __init__(self):
        self.steam = find_steam()
        self.recordings, self.userdir = find_recordings(self.steam)
        self.libmpv = find_libmpv()
        self.output = default_output()
        self.cache = cache_dir()

    @property
    def video_dir(self) -> Path | None:
        return (self.recordings / "video") if self.recordings else None

    @property
    def ok(self) -> bool:
        return bool(self.video_dir and self.video_dir.is_dir())

    def problems(self) -> list[str]:
        out = []
        if not self.steam:
            out.append("Instalacao do Steam nao encontrada.")
        elif not self.recordings:
            out.append("Pasta de gravacoes nao encontrada. Ligue a gravacao em "
                       "Steam > Configuracoes > Gravacao de jogo.")
        elif not self.video_dir.is_dir():
            out.append(f"Sem gravacoes ainda em: {self.recordings}")
        if not self.libmpv:
            out.append("libmpv nao encontrada - instale o mpv.net para ter o player.")
        return out

    def describe(self) -> str:
        return (f"Steam      : {self.steam or '-'}\n"
                f"Gravacoes  : {self.recordings or '-'}\n"
                f"Exportar em: {self.output}\n"
                f"libmpv     : {self.libmpv or 'nao encontrada'}")
