# -*- coding: utf-8 -*-
"""Exportacao de trechos.

Duas correcoes vao embutidas em todo preset que reencoda, e nenhuma e opcional
para quem edita:

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
import time
import subprocess
import threading
import uuid
from pathlib import Path

from .config import Config
from .steam import chunk_list

NOEXEC = {"creationflags": subprocess.CREATE_NO_WINDOW}

TO_LIMITED = "format=yuv420p,scale=in_range=full:out_range=limited"

# Presets rapidos. "opts" e o mesmo dicionario que o modal de exportacao monta,
# entao ajustar um preset a mao e so sobrescrever chaves.
PRESETS = {
    "deliver": {
        "label": "Entrega",
        "short": "H.264 · resolução original\n60 fps constante",
        "hint": "H.264 60 fps constante, resolução original. Abre em qualquer "
                "player e importa limpo no Premiere e no After Effects.",
        "opts": {"codec": "h264", "container": "mp4", "quality": 19,
                 "scale": 0, "fps": 60},
    },
    "raw": {
        "label": "Original",
        "short": "HEVC sem reprocessar\ninstantâneo, sem perda",
        "hint": "Cópia do HEVC sem reprocessar. Instantâneo e sem perda, mas "
                "mantém o framerate variável — evite no Premiere.",
        "opts": {"codec": "copy", "container": "mp4"},
    },
    "compact": {
        "label": "Compacto",
        "short": "H.265 · 1080p\n~4× menor que a Entrega",
        "hint": "H.265 em 1080p com compressão alta. Cerca de 4× menor que a "
                "Entrega — bom para enviar por Discord ou WhatsApp.",
        "opts": {"codec": "hevc", "container": "mp4", "quality": 30,
                 "scale": 1080, "fps": 60},
    },
    "edit": {
        "label": "Edição",
        "short": "DNxHR HQ · MOV\npara grading pesado",
        "hint": "DNxHR HQ para grading pesado e After Effects. ~5,6 GB por minuto.",
        "opts": {"codec": "dnxhr", "container": "mov", "scale": 0, "fps": 60},
    },
}

CUSTOM = "custom"
CUSTOM_META = {
    "label": "Personalizado",
    "short": "Ajuste codec, resolução,\nqualidade e formato",
    "hint": "Abre a janela de configuração para você escolher codec, contêiner, "
            "resolução, taxa de quadros, qualidade e bitrate manualmente.",
    "opts": {},
}


CODECS = {
    "copy":  "Sem reprocessar (cópia)",
    "h264":  "H.264 — compatibilidade máxima",
    "hevc":  "H.265 — metade do tamanho, menos compatível",
    "dnxhr": "DNxHR HQ — para edição",
}

CONTAINERS = {"mp4": "MP4", "mkv": "MKV", "mov": "MOV"}

SCALES = {0: "Original", 1440: "1440p", 1080: "1080p", 720: "720p", 480: "480p"}

FPS_CHOICES = {0: "Original", 60: "60 fps", 30: "30 fps"}


def describe(opts: dict) -> str:
    """Resumo curto da configuracao, do jeito que o painel mostra."""
    codec = {"h264": "H.264", "hevc": "H.265", "copy": "Cópia direta",
             "dnxhr": "DNxHR HQ"}.get(opts.get("codec"), opts.get("codec", "?"))
    if opts.get("codec") == "copy":
        return f"{codec} · {opts.get('container', 'mp4').upper()}"
    res = SCALES.get(int(opts.get("scale") or 0), "Original")
    fps = FPS_CHOICES.get(int(opts.get("fps") or 0), "Original")
    q = (f"{int(opts['bitrate'])} kbps" if opts.get("bitrate")
         else f"CQ {opts.get('quality', 19)}")
    return f"{codec} · {res} · {fps} · {q} · {opts.get('container','mp4').upper()}"


def preset_opts(preset: str) -> dict:
    """Opcoes de um preset, prontas para o modal editar."""
    base = {"codec": "h264", "container": "mp4", "quality": 19,
            "scale": 0, "fps": 60, "bitrate": 0}
    base.update(PRESETS.get(preset, PRESETS["deliver"])["opts"])
    return base


def build_args(opts: dict, start: float, dur: float,
               vfile: Path, afile: Path | None, out: Path) -> list[str]:
    codec = opts.get("codec", "h264")
    scale = int(opts.get("scale") or 0)
    fps = int(opts.get("fps") or 0)
    bitrate = int(opts.get("bitrate") or 0)
    quality = int(opts.get("quality", 19))

    a = ["ffmpeg", "-y", "-hide_banner", "-v", "warning", "-stats",
         "-progress", "pipe:1", "-nostdin"]
    if codec in ("h264", "hevc"):
        a += ["-hwaccel", "cuda"]
    a += ["-i", str(vfile)]
    if afile:
        a += ["-i", str(afile)]
    if start > 0.01:
        a += ["-ss", f"{start:.3f}"]
    if dur > 0:
        a += ["-t", f"{dur:.3f}"]

    if codec == "copy":
        return a + ["-c", "copy"] + _tail(out)

    vf = TO_LIMITED
    if scale:
        vf += f",scale=-2:{scale}"
    if codec == "dnxhr":
        vf += ",format=yuv422p"
    a += ["-vf", vf]
    if fps:
        a += ["-fps_mode", "cfr", "-r", str(fps)]

    if codec == "h264":
        a += ["-c:v", "h264_nvenc", "-preset", "p6", "-profile:v", "high"]
    elif codec == "hevc":
        a += ["-c:v", "hevc_nvenc", "-preset", "p6", "-tag:v", "hvc1"]
    elif codec == "dnxhr":
        a += ["-c:v", "dnxhd", "-profile:v", "dnxhr_hq"]

    if codec in ("h264", "hevc"):
        if bitrate:                       # bitrate fixo pedido pelo usuario
            a += ["-rc", "vbr", "-b:v", f"{bitrate}k",
                  "-maxrate", f"{int(bitrate * 1.5)}k", "-bufsize", f"{bitrate * 2}k"]
        else:
            a += ["-rc", "vbr", "-cq", str(quality), "-b:v", "0"]
        a += ["-color_range", "tv", "-colorspace", "bt709",
              "-color_primaries", "bt709", "-color_trc", "bt709"]

    if afile:
        a += ["-c:a", "pcm_s16le"] if codec == "dnxhr" else ["-c:a", "aac", "-b:a", "192k"]
    return a + _tail(out)


def _tail(out: Path) -> list[str]:
    return (["-movflags", "+faststart", str(out)] if out.suffix == ".mp4"
            else [str(out)])


# MB/s medidos exportando o mesmo trecho de gameplay em varias combinacoes; a
# curva antiga chutava e errava por quase 5x. Referencia: h264 em cq 23.
_BASE_MB_S = {2160: 10.5, 1440: 4.96, 1080: 2.99, 720: 1.45, 480: 0.65}
_CODEC_F = {"h264": 1.0, "hevc": 0.65}
_Q_STEP = {"h264": 1.114, "hevc": 1.086}     # por ponto de cq abaixo de 23


def estimate_mb(opts: dict, seconds: float, src_mb_s: float | None = None) -> float:
    """Tamanho final aproximado, para orientar a escolha antes de exportar."""
    codec = opts.get("codec", "h264")
    if codec == "copy":
        # sem reprocessar o tamanho e o do proprio material
        return seconds * (src_mb_s if src_mb_s else 3.9)
    if codec == "dnxhr":
        # DNxHR e intra-frame: o tamanho acompanha pixels x quadros, nao a cena.
        # Ancora: 1440p60 medido em 93 MB/s -> 0,441 byte por pixel.
        h = int(opts.get("scale") or 0) or 1440
        fps = int(opts.get("fps") or 0) or 60
        return seconds * (h * 16 / 9) * h * fps * 0.441 / 1048576
    if opts.get("bitrate"):
        return seconds * int(opts["bitrate"]) / 8 / 1024

    scale = int(opts.get("scale") or 0) or 1440
    base = _BASE_MB_S.get(scale)
    if base is None:                            # resolucao fora da tabela: por area
        base = _BASE_MB_S[1080] * (scale / 1080) ** 2
    q = int(opts.get("quality", 19))
    f = _CODEC_F.get(codec, 1.0) * (_Q_STEP.get(codec, 1.1) ** (23 - q))
    return seconds * base * f


class Jobs:
    """Fila de exportacoes, com progresso do -progress do ffmpeg e cancelamento."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.items: dict[str, dict] = {}
        self.procs: dict[str, subprocess.Popen] = {}
        self.lock = threading.Lock()

    def _upd(self, jid: str, **kw):
        with self.lock:
            self.items.setdefault(jid, {}).update(kw)

    def snapshot(self) -> dict:
        with self.lock:
            return {k: dict(v) for k, v in self.items.items()}

    def get(self, jid: str) -> dict:
        with self.lock:
            return dict(self.items.get(jid, {}))

    def clear_finished(self):
        with self.lock:
            for k in [k for k, v in self.items.items()
                      if v.get("status") in ("pronto", "erro", "cancelado")]:
                del self.items[k]

    def cancel(self, jid: str) -> dict:
        """Mata o ffmpeg e apaga o arquivo pela metade."""
        with self.lock:
            job = self.items.get(jid)
            proc = self.procs.get(jid)
        if not job:
            return {"error": "trabalho nao encontrado"}
        if job.get("status") in ("pronto", "erro", "cancelado"):
            return {"error": "ja terminou"}

        # A marca vem ANTES do kill: matar primeiro abre uma corrida em que o _run
        # sai do p.wait(), le canceled=False e conclui "erro" em vez de cancelado.
        self._upd(jid, canceled=True, status="cancelando")
        if proc and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        # A remocao do arquivo pela metade acontece no finally do _run, que so
        # roda depois do ffmpeg soltar o descritor.
        return {"ok": True}

    def submit(self, sid: str, preset: str, start: float, dur: float,
               name: str, opts: dict | None = None) -> str:
        o = preset_opts(preset)
        if opts:
            o.update({k: v for k, v in opts.items() if v is not None})
        jid = uuid.uuid4().hex
        self._upd(jid, id=jid, session=sid, preset=preset, label=name,
                  status="na fila", pct=0, opts=o, canceled=False,
                  seconds=dur, started=time.time(), elapsed=0.0, eta=None,
                  bytes_now=0, estimate_mb=round(estimate_mb(o, dur), 1),
                  summary=describe(o))
        threading.Thread(target=self._run, daemon=True,
                         args=(jid, sid, start, dur, name, o)).start()
        return jid

    def _run(self, jid, sid, start, dur, name, opts):
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
        # Um trecho fora do material vira "ffmpeg saiu com codigo 0": o ffmpeg
        # roda sem entrada e devolve arquivo vazio com sucesso. Reportado como
        # erro do job (nao levantado aqui, que fica fora do try).
        if not vsel:
            self._upd(jid, status="erro",
                      error=f"O trecho começa em {start:.0f}s, mas esta gravação "
                            f"só tem {len(vch) * seg:.0f}s.")
            return

        tmp = Path(os.environ["TEMP"]) / f"steamclipper_{jid[:8]}"
        tmp.mkdir(parents=True, exist_ok=True)
        out = None

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
            if self.get(jid).get("canceled"):
                raise InterruptedError

            outdir = Path(opts.get("outdir") or cfg.output)
            outdir.mkdir(parents=True, exist_ok=True)
            ext = opts.get("container", "mp4")
            safe = re.sub(r'[<>:"/\\|?*]', "", name).strip() or sid
            tag = f"_{int(start)}s-{int(start + dur)}s" if dur > 0 else ""
            out = outdir / f"{safe}{tag}.{ext}"
            i = 2
            while out.exists():
                out = outdir / f"{safe}{tag}_{i}.{ext}"
                i += 1

            total = dur if dur > 0 else len(vsel) * seg
            self._upd(jid, status="processando", out=str(out))

            p = subprocess.Popen(build_args(opts, fine, dur, vfile, afile, out),
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 text=True, bufsize=1, **NOEXEC)
            with self.lock:
                self.procs[jid] = p
            t0 = time.time()
            for line in p.stdout:
                if not line.startswith("out_time_ms="):
                    continue
                try:
                    secs = int(line.split("=", 1)[1]) / 1e6
                except ValueError:
                    continue
                frac = (secs / total) if total else 0
                elapsed = time.time() - t0
                # ETA pela velocidade media ate aqui; so vale depois de 2% para
                # nao mostrar numeros absurdos nos primeiros quadros.
                eta = (elapsed / frac - elapsed) if frac > 0.02 else None
                try:
                    now_bytes = out.stat().st_size
                except OSError:
                    now_bytes = 0
                self._upd(jid, pct=min(99, round(frac * 100)), elapsed=elapsed,
                          eta=eta, bytes_now=now_bytes,
                          speed=(secs / elapsed) if elapsed > 0.5 else None)
            p.wait()

            if self.get(jid).get("canceled"):
                raise InterruptedError
            if p.returncode == 0 and out.is_file() and out.stat().st_size > 1024:
                self._upd(jid, status="pronto", pct=100, size=out.stat().st_size)
            else:
                self._upd(jid, status="erro", error=f"ffmpeg saiu com codigo {p.returncode}")
        except InterruptedError:
            self._upd(jid, status="cancelado", pct=0)
        except Exception as e:                                 # noqa: BLE001
            self._upd(jid, status="erro", error=str(e))
        finally:
            with self.lock:
                self.procs.pop(jid, None)
            # Arquivo pela metade nao serve para nada: some junto com o cancelamento
            # ou com o erro.
            if out and self.get(jid).get("status") in ("cancelado", "erro"):
                try:
                    out.unlink(missing_ok=True)
                except OSError:
                    pass
            for f in tmp.glob("*"):
                f.unlink(missing_ok=True)
            try:
                tmp.rmdir()
            except OSError:
                pass
