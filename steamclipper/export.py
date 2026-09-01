# -*- coding: utf-8 -*-
"""Exportacao de trechos.

Duas correcoes vao embutidas nos presets, e nenhuma e opcional para quem edita:

  VFR -> CFR       O Steam grava com framerate variavel (quadros de 14,7 a 15,9 ms).
                   Premiere e After Effects dessincronizam o audio com VFR.

  full -> limited  O stream e yuvj420p / color_range=pc (luma 0-255) e os editores
                   assumem 16-235, estourando o contraste. A pegadinha: o filtro
                   scale sozinho NAO converte - precisa de format=yuv420p junto,
                   senao a saida continua yuvj420p e nada acontece.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import uuid
from pathlib import Path

from .config import Config
from .steam import chunk_list

NOEXEC = {"creationflags": subprocess.CREATE_NO_WINDOW}

TO_LIMITED = "format=yuv420p,scale=in_range=full:out_range=limited"

PRESETS = {
    "deliver": {
        "label": "Entrega",
        "ext": "mp4",
        "hint": "H.264 60 fps constante. Abre em qualquer player e importa limpo "
                "no Premiere e no After Effects.",
    },
    "raw": {
        "label": "Original",
        "ext": "mp4",
        "hint": "Cópia do HEVC sem reprocessar. Instantâneo e sem perda, mas "
                "mantém o framerate variável — evite no Premiere.",
    },
    "edit": {
        "label": "Edição",
        "ext": "mov",
        "hint": "DNxHR HQ para grading pesado e After Effects. ~5,6 GB por minuto.",
    },
}


def build_args(preset: str, start: float, dur: float, quality: int,
               vfile: Path, afile: Path | None, out: Path) -> list[str]:
    a = ["ffmpeg", "-y", "-hide_banner", "-v", "warning", "-stats",
         "-progress", "pipe:1", "-nostdin"]
    if preset != "edit":
        a += ["-hwaccel", "cuda"]
    a += ["-i", str(vfile)]
    if afile:
        a += ["-i", str(afile)]
    if start > 0.01:
        a += ["-ss", f"{start:.3f}"]
    if dur > 0:
        a += ["-t", f"{dur:.3f}"]

    if preset == "raw":
        a += ["-c", "copy"]
    elif preset == "edit":
        a += ["-fps_mode", "cfr", "-r", "60",
              "-vf", f"{TO_LIMITED},format=yuv422p",
              "-c:v", "dnxhd", "-profile:v", "dnxhr_hq"]
        if afile:
            a += ["-c:a", "pcm_s16le"]
    else:                                    # deliver
        a += ["-fps_mode", "cfr", "-r", "60", "-vf", TO_LIMITED,
              "-c:v", "h264_nvenc", "-preset", "p6", "-rc", "vbr",
              "-cq", str(quality), "-b:v", "0", "-profile:v", "high",
              "-color_range", "tv", "-colorspace", "bt709",
              "-color_primaries", "bt709", "-color_trc", "bt709"]
        if afile:
            a += ["-c:a", "aac", "-b:a", "192k"]

    if out.suffix == ".mp4":
        a += ["-movflags", "+faststart"]
    return a + [str(out)]


class Jobs:
    """Fila de exportacoes com progresso lido do -progress do ffmpeg."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.items: dict[str, dict] = {}
        self.lock = threading.Lock()

    def _upd(self, jid: str, **kw):
        with self.lock:
            self.items.setdefault(jid, {}).update(kw)

    def snapshot(self) -> dict:
        with self.lock:
            return {k: dict(v) for k, v in self.items.items()}

    def submit(self, sid: str, preset: str, start: float, dur: float,
               quality: int, name: str) -> str:
        jid = uuid.uuid4().hex
        self._upd(jid, id=jid, session=sid, preset=preset, label=name,
                  status="na fila", pct=0)
        threading.Thread(target=self._run, daemon=True,
                         args=(jid, sid, preset, start, dur, quality, name)).start()
        return jid

    def _run(self, jid, sid, preset, start, dur, quality, name):
        cfg = self.cfg
        d = cfg.video_dir / sid
        vch, ach = chunk_list(d, 0), chunk_list(d, 1)
        seg = Config.SEGMENT_SECONDS

        # Recorta na granularidade do segmento; o resto (<3s) fica com o -ss do
        # ffmpeg. Sem isso, tirar 90s de uma sessao de 1h montaria os 9 GB inteiros.
        skip = max(0, int(start // seg))
        fine = start - skip * seg
        take = int(-(-(fine + dur) // seg)) + 1 if dur > 0 else len(vch)
        vsel, asel = vch[skip:skip + take], ach[skip:skip + take]

        tmp = Path(os.environ["TEMP"]) / f"steamclipper_{jid[:8]}"
        tmp.mkdir(parents=True, exist_ok=True)

        def cat(init: Path, parts, dest: Path):
            with open(dest, "wb") as w:
                w.write(init.read_bytes())
                for p in parts:
                    w.write(p.read_bytes())

        try:
            self._upd(jid, status="montando", pct=0)
            vfile = tmp / "v.mp4"
            cat(d / "init-stream0.m4s", vsel, vfile)
            afile = None
            if asel:
                afile = tmp / "a.mp4"
                cat(d / "init-stream1.m4s", asel, afile)

            cfg.output.mkdir(parents=True, exist_ok=True)
            ext = PRESETS.get(preset, PRESETS["deliver"])["ext"]
            safe = re.sub(r'[<>:"/\\|?*]', "", name).strip() or sid
            tag = f"_{int(start)}s-{int(start + dur)}s" if dur > 0 else ""
            out = cfg.output / f"{safe}{tag}_{preset}.{ext}"
            i = 2
            while out.exists():
                out = cfg.output / f"{safe}{tag}_{preset}_{i}.{ext}"
                i += 1

            total = dur if dur > 0 else len(vsel) * seg
            self._upd(jid, status="processando", out=str(out))

            p = subprocess.Popen(
                build_args(preset, fine, dur, quality, vfile, afile, out),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1, **NOEXEC)
            for line in p.stdout:
                if line.startswith("out_time_ms="):
                    try:
                        secs = int(line.split("=", 1)[1]) / 1e6
                        self._upd(jid, pct=min(99, round(secs / total * 100)) if total else 0)
                    except ValueError:
                        pass
            p.wait()

            if p.returncode == 0 and out.is_file() and out.stat().st_size > 1024:
                self._upd(jid, status="pronto", pct=100, size=out.stat().st_size)
            else:
                self._upd(jid, status="erro", error=f"ffmpeg saiu com codigo {p.returncode}")
        except Exception as e:                                 # noqa: BLE001
            self._upd(jid, status="erro", error=str(e))
        finally:
            for f in tmp.glob("*"):
                f.unlink(missing_ok=True)
            try:
                tmp.rmdir()
            except OSError:
                pass
