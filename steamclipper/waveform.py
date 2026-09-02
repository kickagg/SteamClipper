# -*- coding: utf-8 -*-
"""Envelope de audio para desenhar a forma de onda na linha do tempo.

O audio e decodificado uma vez para PCM mono em taxa baixa e reduzido a um pico
por intervalo. A 200 Hz (5 ms por pico) uma gravacao de 92 minutos vira ~1,1 MB,
o que sustenta zoom alto sem redecodificar: com 150 000 colunas de tela ainda
sobra amostra. A extracao leva ~9 s para 92 min e fica em cache no disco.
"""

from __future__ import annotations

import struct
import subprocess
import threading
from pathlib import Path

from .config import Config

NOEXEC = {"creationflags": subprocess.CREATE_NO_WINDOW}

PEAKS_HZ = 200                  # picos por segundo guardados em cache
_CACHE_VERSION = 1              # muda se o formato do arquivo mudar

_jobs: dict[str, threading.Thread] = {}
_lock = threading.Lock()


def cache_path(cfg: Config, sid: str) -> Path:
    return cfg.cache / f"{sid}_wave{_CACHE_VERSION}.bin"


def load(cfg: Config, sid: str) -> bytes | None:
    """Picos ja extraidos, ou None se ainda nao existem."""
    p = cache_path(cfg, sid)
    try:
        return p.read_bytes() if p.is_file() else None
    except OSError:
        return None


def extract(cfg: Config, sid: str, port: int, on_done=None) -> None:
    """Extrai os picos em segundo plano; chama on_done(bytes) ao terminar.

    Uma extracao por sessao de cada vez - clicar em varias gravacoes seguidas nao
    dispara ffmpeg concorrente para a mesma.
    """
    with _lock:
        if sid in _jobs and _jobs[sid].is_alive():
            return
        t = threading.Thread(target=_work, args=(cfg, sid, port, on_done), daemon=True)
        _jobs[sid] = t
        t.start()


def _work(cfg: Config, sid: str, port: int, on_done) -> None:
    dest = cache_path(cfg, sid)
    if dest.is_file():
        if on_done:
            on_done(dest.read_bytes())
        return

    url = f"http://127.0.0.1:{port}/api/stream?session={sid}&stream=1"
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", url, "-ac", "1", "-ar", str(PEAKS_HZ),
             "-f", "s16le", "-"],
            capture_output=True, timeout=600, **NOEXEC)
        raw = r.stdout
        if not raw:
            return
    except (subprocess.SubprocessError, OSError):
        return

    # int16 -> um byte por pico (0-255). O sinal nao importa para a silhueta.
    n = len(raw) // 2
    vals = struct.unpack(f"<{n}h", raw[:n * 2])
    peaks = bytes(min(255, abs(v) >> 7) for v in vals)

    try:
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(peaks)
        tmp.replace(dest)
    except OSError:
        return
    if on_done:
        on_done(peaks)


def column_peaks(peaks: bytes, t0: float, t1: float, columns: int) -> list[int]:
    """Reduz o trecho [t0, t1] a uma altura por coluna de tela (0-255).

    Usa o maximo de cada intervalo, nao a media: picos curtos - um tiro, um
    impacto - somem numa media e sao justamente o que se procura ao cortar.
    """
    if not peaks or columns <= 0:
        return []
    i0 = max(0, int(t0 * PEAKS_HZ))
    i1 = min(len(peaks), max(i0 + 1, int(t1 * PEAKS_HZ)))
    span = i1 - i0
    if span <= 0:
        return [0] * columns

    out = []
    for c in range(columns):
        a = i0 + span * c // columns
        b = i0 + span * (c + 1) // columns
        if b <= a:
            b = a + 1
        out.append(max(peaks[a:b], default=0))
    return out
