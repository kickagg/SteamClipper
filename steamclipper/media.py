# -*- coding: utf-8 -*-
"""Os segmentos DASH vistos como um arquivo so, e as miniaturas.

As gravacoes sao segmentos MPEG-DASH de 3s: init-stream0/1.m4s guardam o cabecalho
(ftyp+moov) de video e audio, e cada chunk-streamN-NNNNN.m4s guarda um fragmento
(moof+mdat). init + chunks concatenados formam um fMP4 valido.

O session.mpd NAO e usado de proposito: quando o buffer circular poda os segmentos
antigos, o manifesto continua declarando startNumber="1" e a duracao original, entao
qualquer player que o siga pede arquivos que nao existem mais. Ler os chunks reais
do disco funciona nos dois casos.
"""

from __future__ import annotations

import struct
import subprocess
import threading
import uuid
from pathlib import Path

from .config import Config
from .steam import chunk_list

NOEXEC = {"creationflags": subprocess.CREATE_NO_WINDOW}


# --------------------------------------------------------------- boxes do MP4
def _boxes(buf: bytes, base: int = 0):
    o = 0
    while o + 8 <= len(buf):
        size, typ = struct.unpack_from(">I4s", buf, o)
        if size == 0:
            size = len(buf) - o
        if size < 8 or o + size > len(buf):
            break
        yield typ.decode("latin1"), base + o, size
        o += size


def _find(buf: bytes, path: tuple[str, ...], base: int = 0):
    """Acha um box aninhado (ex: moov/trak/mdia/mdhd) -> (offset absoluto, tamanho)."""
    for typ, off, size in _boxes(buf, base):
        if typ != path[0]:
            continue
        if len(path) == 1:
            return off, size
        inner = buf[off - base + 8: off - base + size]
        r = _find(inner, path[1:], off + 8)
        if r:
            return r
    return None


def patch_init(init: bytes, seconds: float) -> bytes:
    """Escreve a duracao real em mvhd/tkhd/mdhd.

    Num init DASH esses campos vem zerados (e um fMP4 ao vivo). Sem eles o player
    nao sabe o tamanho do video e nao deixa navegar.
    """
    b = bytearray(init)
    moov = _find(b, ("moov",))
    if not moov:
        return bytes(b)
    moff, msize = moov
    body = bytes(b[moff + 8: moff + msize])

    def set_dur(path, ts_rel, dur_rel):
        r = _find(body, path, moff + 8)
        if not r:
            return
        off, _ = r
        if b[off + 8] != 0:                # so tratamos version 0
            return
        ts = struct.unpack_from(">I", b, off + ts_rel)[0]
        if ts:
            struct.pack_into(">I", b, off + dur_rel,
                             min(0xFFFFFFFE, int(seconds * ts)))

    set_dur(("mvhd",), 20, 24)
    set_dur(("trak", "mdia", "mdhd"), 20, 24)

    # tkhd v0: 8 header + 4 version/flags + 8 datas + 4 trackID + 4 reservado
    r = _find(body, ("trak", "tkhd"), moff + 8)
    if r and b[r[0] + 8] == 0:
        mv = _find(b, ("moov", "mvhd"))
        if mv:
            mts = struct.unpack_from(">I", b, mv[0] + 20)[0]
            if mts:
                struct.pack_into(">I", b, r[0] + 28,
                                 min(0xFFFFFFFE, int(seconds * mts)))
    return bytes(b)


def build_sidx(init: bytes, sizes: list[int], seg: float) -> bytes:
    """Indice tempo -> byte offset, uma referencia por chunk.

    Como sabemos o tamanho exato de cada chunk e que cada um dura 3s, o indice sai
    perfeito: o seek fica instantaneo mesmo numa sessao de 27 GB.
    """
    mdhd = _find(init, ("moov", "trak", "mdia", "mdhd"))
    ts = 1000000
    if mdhd and init[mdhd[0] + 8] == 0:
        ts = struct.unpack_from(">I", init, mdhd[0] + 20)[0] or 1000000

    body = struct.pack(">BBBB", 0, 0, 0, 0)          # version + flags
    body += struct.pack(">II", 1, ts)                # reference_ID, timescale
    body += struct.pack(">II", 0, 0)                 # earliest_pt, first_offset
    body += struct.pack(">HH", 0, len(sizes))        # reservado, reference_count
    dur = int(seg * ts)
    for s in sizes:
        body += struct.pack(">III", s & 0x7FFFFFFF, dur, 0x90000000)
    return struct.pack(">I4s", len(body) + 8, b"sidx") + body


# ------------------------------------------------------- concatenacao virtual
class VirtualFile:
    """init remontado + chunks, endereçavel por byte sem montar nada em disco."""

    def __init__(self, cfg: Config, sid: str, stream: int = 0):
        d = cfg.video_dir / sid
        init_p = d / f"init-stream{stream}.m4s"
        if not init_p.is_file():
            raise FileNotFoundError(init_p)

        chunks = chunk_list(d, stream)
        sizes = [p.stat().st_size for p in chunks]
        header = patch_init(init_p.read_bytes(), len(chunks) * Config.SEGMENT_SECONDS)
        header += build_sidx(header, sizes, Config.SEGMENT_SECONDS)

        self.parts: list[tuple[int, object, int]] = [(0, header, len(header))]
        off = len(header)
        for p, n in zip(chunks, sizes):
            self.parts.append((off, p, n))
            off += n
        self.size = off

    def read(self, start: int, end: int):
        """Le [start, end], indo direto ao arquivo que contem cada offset."""
        for off, src, n in self.parts:
            if off + n <= start:
                continue
            if off > end:
                break
            a = max(0, start - off)
            b = min(n - 1, end - off)
            if isinstance(src, bytes):
                yield src[a:b + 1]
                continue
            with open(src, "rb") as f:
                f.seek(a)
                want = b - a + 1
                while want > 0:
                    buf = f.read(min(262144, want))
                    if not buf:
                        break
                    want -= len(buf)
                    yield buf


_vcache: dict[tuple[str, int], VirtualFile] = {}
_vlock = threading.Lock()


def virtual(cfg: Config, sid: str, stream: int = 0) -> VirtualFile:
    key = (sid, stream)
    with _vlock:
        v = _vcache.get(key)
        if v is None:
            v = _vcache[key] = VirtualFile(cfg, sid, stream)
        return v


# ---------------------------------------------------------------- miniaturas
#
# O formato importa por causa de quem consome: o navegador aceita JPEG, mas o
# tkinter sem PIL so le PNG/GIF/PPM. Por isso a versao Desktop pede fmt="png".

def thumb(cfg: Config, sid: str, t: float, width: int = 426,
          fmt: str = "jpg") -> Path:
    out = cfg.cache / f"{sid}_{int(t)}_{width}.{fmt}"
    if out.is_file():
        return out
    d = cfg.video_dir / sid
    chunks = chunk_list(d, 0)
    if not chunks:
        raise FileNotFoundError("sessao sem chunks de video")
    idx = max(0, min(len(chunks) - 1, int(t // Config.SEGMENT_SECONDS)))
    tmp = cfg.cache / f"_{uuid.uuid4().hex}.mp4"
    try:
        tmp.write_bytes((d / "init-stream0.m4s").read_bytes() + chunks[idx].read_bytes())
        args = ["ffmpeg", "-y", "-v", "error", "-i", str(tmp),
                "-frames:v", "1", "-vf", f"scale={width}:-2"]
        if fmt == "jpg":
            args += ["-q:v", "5"]
        subprocess.run(args + [str(out)], check=True, **NOEXEC)
    finally:
        tmp.unlink(missing_ok=True)
    return out


def strip(cfg: Config, sid: str, n: int = 16, width: int = 426,
          fmt: str = "jpg") -> Path:
    """Faixa unica com n quadros para a linha do tempo.

    Uma imagem em vez de n requisicoes: o navegador abre no maximo 6 conexoes por
    origem, e a faixa antiga sozinha consumia 16 delas.
    """
    out = cfg.cache / f"{sid}_strip{n}_{width}.{fmt}"
    if out.is_file():
        return out
    chunks = chunk_list(cfg.video_dir / sid, 0)
    if not chunks:
        raise FileNotFoundError("sessao sem chunks de video")

    parts = [thumb(cfg, sid,
                   min(len(chunks) - 1, int((i + 0.5) / n * len(chunks)))
                   * Config.SEGMENT_SECONDS, width, fmt)
             for i in range(n)]

    args = ["ffmpeg", "-y", "-v", "error"]
    for p in parts:
        args += ["-i", str(p)]
    args += ["-filter_complex", f"hstack=inputs={len(parts)}"]
    if fmt == "jpg":
        args += ["-q:v", "6"]
    subprocess.run(args + [str(out)], check=True, **NOEXEC)
    return out
