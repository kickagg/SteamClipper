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

# "Original" prometia preservar o VFR e nao preservava - reencodar sempre gera
# framerate constante. O rotulo agora diz o que realmente acontece.
FPS_CHOICES = {0: "Do material (constante)", 60: "60 fps", 30: "30 fps"}


# DNxHR e um codec de producao: o MP4 nao o carrega e o ffmpeg recusa a combinacao.
CODEC_CONTAINERS = {
    "copy":  ("mp4", "mkv", "mov"),
    "h264":  ("mp4", "mkv", "mov"),
    "hevc":  ("mp4", "mkv", "mov"),
    "dnxhr": ("mov", "mkv"),
}

MIN_DURATION = 0.5          # abaixo disso o arquivo sai sem conteudo util
QUALITY_RANGE = (0, 51)     # faixa que o NVENC aceita em -cq
MAX_BITRATE = 400_000       # kbps; acima disso o encoder ignora o alvo
MAX_NAME = 120              # caracteres do nome, antes do sufixo e da extensao

# Nomes que o Windows reserva desde o DOS: um arquivo com esse nome base e
# inacessivel por caminho normal, mesmo com extensao.
_RESERVED = {"CON", "PRN", "AUX", "NUL", "COM0", "COM1", "COM2", "COM3", "COM4",
             "COM5", "COM6", "COM7", "COM8", "COM9", "LPT0", "LPT1", "LPT2",
             "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"}

_MEDIA_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv", ".flv"}


def timecode(seconds: float, compact: bool = True) -> str:
    """1512s -> '25m12s' (para nome de arquivo) ou '25:12' (para exibicao)."""
    s = max(0, int(round(seconds)))
    h, m, x = s // 3600, (s % 3600) // 60, s % 60
    if compact:
        return (f"{h}h{m:02d}m{x:02d}s" if h else f"{m}m{x:02d}s")
    return (f"{h}:{m:02d}:{x:02d}" if h else f"{m}:{x:02d}")


def safe_name(name: str, fallback: str = "clipe") -> str:
    """Nome de arquivo utilizavel no Windows, preservando o que da para preservar.

    Trata o que a auditoria encontrou: caracteres proibidos viravam espacos duplos,
    nomes reservados passavam, extensao de video no nome duplicava, nome vazio caia
    no id tecnico da sessao e 180 caracteres geravam um caminho perto do limite.
    """
    n = str(name or "").strip()
    n = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", n)   # proibidos viram espaco
    n = re.sub(r"\s+", " ", n).strip()             # sem espacos duplicados
    n = n.strip(". ")                              # sem ponto/espaco nas pontas

    # "clipe.mp4" nao deve virar "clipe.mp4_25m12s.mp4"
    stem = Path(n)
    if stem.suffix.lower() in _MEDIA_EXT:
        n = stem.stem.strip()

    if len(n) > MAX_NAME:
        n = n[:MAX_NAME].rstrip(". ")
    if n.upper() in _RESERVED or n.split(".")[0].upper() in _RESERVED:
        n = f"{n}_"
    return n or fallback


def output_name(name: str, start: float, dur: float, ext: str,
                with_suffix: bool = True, fallback: str = "clipe") -> str:
    """Nome final. O sufixo de trecho e opcional e usa timecode, nao segundos."""
    base = safe_name(name, fallback)
    if with_suffix:
        base = f"{base} [{timecode(start)}-{timecode(start + dur)}]"
    return f"{base}.{ext}"


def sanitize(opts: dict) -> dict:
    """Prende as opcoes em valores que o ffmpeg aceita.

    A interface ja limita as escolhas, mas o nucleo e chamado tambem por presets
    salvos em disco e pela versao Browser - a auditoria passou cq 99, cq -5,
    bitrate negativo e escala negativa direto para a linha de comando.
    """
    o = dict(opts)
    codec = o.get("codec", "h264")
    if codec not in CODECS:
        codec = "h264"
    o["codec"] = codec

    cont = o.get("container", "mp4")
    permitidos = CODEC_CONTAINERS[codec]
    if cont not in permitidos:
        cont = permitidos[0]
    o["container"] = cont

    scale = int(o.get("scale") or 0)
    o["scale"] = scale if scale in SCALES else 0

    fps = int(o.get("fps") or 0)
    o["fps"] = fps if fps in FPS_CHOICES else 0

    q = int(o.get("quality", 19))
    o["quality"] = max(QUALITY_RANGE[0], min(QUALITY_RANGE[1], q))

    br = int(o.get("bitrate") or 0)
    o["bitrate"] = max(0, min(MAX_BITRATE, br))
    return o


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
    # Sem -r o encoder segue o framerate do material, mas sempre em CFR:
    # reencodar normaliza os tempos em qualquer conteiner, com NVENC ou libx264
    # (medido: entrada com 194 duracoes distintas sai com 1). Quem precisa do VFR
    # original tem de usar o preset Original, que copia sem reprocessar.

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


# MB/s em h264 CQ 23, media de 11 trechos de duas gravacoes. A calibracao antiga
# usava um unico trecho e ficava 35% acima da media real - um trecho de acao nao
# representa a gravacao inteira.
_BASE_MB_S = {2160: 7.75, 1440: 3.66, 1080: 2.21, 720: 1.07, 480: 0.48}
# O ganho do H.265 sobre o H.264 encolhe conforme a resolucao cai - em 480p os
# dois praticamente empatam. Uma constante unica errava 46% na ponta de baixo.
_HEVC_F = {2160: 0.55, 1440: 0.58, 1080: 0.74, 720: 0.89, 480: 0.94}
_CODEC_F = {"h264": 1.0, "hevc": 0.58}
_Q_STEP = {"h264": 1.114, "hevc": 1.086}     # por ponto de cq abaixo de 23


# Acima de CQ 19 o encoder para de crescer: 4 pontos rendem so ~5% de arquivo a
# mais. Sem este teto a curva exponencial errava +75% em CQ 15.
_Q_SATURATION = 19

# Quanto o tamanho varia com a cena, medido em 11 trechos de duas gravacoes na
# mesma configuracao: de 0,27 a 1,55 MB/s. Tentamos prever isso pelo bitrate do
# material original e nao funciona - a gravacao do Steam tem bitrate quase
# constante, e a correlacao com o tamanho final ficou em 0,07. Como a cena so se
# conhece codificando, a estimativa e uma FAIXA, nao um numero.
_SPREAD_LOW, _SPREAD_HIGH = 0.40, 1.45


def estimate_mb(opts: dict, seconds: float, src_mb_s: float | None = None) -> float:
    """Tamanho final provavel, no centro da faixa. Ver estimate_range."""
    codec = opts.get("codec", "h264")
    if codec == "copy":
        # Copia nao recodifica: o tamanho e o do proprio material, e ai o bitrate
        # da fonte e exato em vez de aproximado.
        return seconds * (src_mb_s if src_mb_s else 3.9)
    if codec == "dnxhr":
        # DNxHR e intra-frame: o tamanho acompanha pixels x quadros, nao a cena -
        # por isso este ramo e preciso (erro de 1,3% na auditoria).
        h = int(opts.get("scale") or 0) or 1440
        fps = int(opts.get("fps") or 0) or 60
        return seconds * (h * 16 / 9) * h * fps * 0.441 / 1048576

    scale = int(opts.get("scale") or 0) or 1440
    base = _BASE_MB_S.get(scale)
    if base is None:                            # resolucao fora da tabela: por area
        base = _BASE_MB_S[1080] * (scale / 1080) ** 2

    if opts.get("bitrate"):
        # O encoder nao gasta bits que a cena nao pede: em alvos altos ele fica
        # bem abaixo do pedido (100 Mbps entregaram 55 na auditoria). O teto e o
        # bitrate; o piso, o que a cena costuma exigir.
        teto = seconds * int(opts["bitrate"]) / 8 / 1024
        natural = seconds * base * 2.4
        return min(teto, max(natural, teto * 0.45))

    q = max(_Q_SATURATION, int(opts.get("quality", 19)))
    cf = _HEVC_F.get(scale, _CODEC_F["hevc"]) if codec == "hevc" else 1.0
    f = cf * (_Q_STEP.get(codec, 1.1) ** (23 - q))
    if int(opts.get("fps") or 0) == 30:
        f *= 0.62                               # medido: 3,505 contra 6,244 MB
    return seconds * base * f


def estimate_range(opts: dict, seconds: float,
                   src_mb_s: float | None = None) -> tuple[float, float]:
    """(minimo, maximo) provaveis. Cena parada fica embaixo, acao em cima."""
    mid = estimate_mb(opts, seconds, src_mb_s)
    codec = opts.get("codec", "h264")
    if codec in ("copy", "dnxhr"):
        return mid * 0.97, mid * 1.03           # nao dependem da cena
    if opts.get("bitrate"):
        return mid * 0.70, mid * 1.40
    return mid * _SPREAD_LOW, mid * _SPREAD_HIGH


def human_mb(mb: float) -> str:
    """230 -> '230 MB' · 8623 -> '8,4 GB' (padrao brasileiro)."""
    if mb >= 1024:
        return f"{mb / 1024:.1f}".replace(".", ",") + " GB"
    if mb >= 10:
        return f"{mb:.0f} MB"
    return f"{mb:.1f}".replace(".", ",") + " MB"


def format_estimate(opts: dict, seconds: float,
                    src_mb_s: float | None = None) -> str:
    """Texto pronto para a interface, em faixa sempre que a cena pesar."""
    lo, hi = estimate_range(opts, seconds, src_mb_s)
    if hi - lo < max(1.0, hi * 0.10):        # codec previsivel: um numero basta
        return "~" + human_mb((lo + hi) / 2)
    return f"{human_mb(lo)} a {human_mb(hi)}"


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
        outdir = (opts or {}).get("outdir")
        o = preset_opts(preset)
        if opts:
            o.update({k: v for k, v in opts.items() if v is not None})
        o = sanitize(o)                 # cq, bitrate, escala e contêiner válidos
        if outdir:
            o["outdir"] = outdir

        # O trecho e prendido ao material disponivel ANTES de virar job, para que
        # o nome e a estimativa descrevam o que sai, nao o que foi pedido.
        disponivel = len(chunk_list(self.cfg.video_dir / sid, 0)) * Config.SEGMENT_SECONDS
        start = max(0.0, min(float(start), max(0.0, disponivel - MIN_DURATION)))
        dur = float(dur)

        jid = uuid.uuid4().hex
        # Duracao zero significava "ate o fim": um trecho nao marcado exportava a
        # gravacao inteira em silencio (2,5 GB e 3,5 minutos na auditoria). Quem
        # quiser a sessao toda passa a duracao dela explicitamente.
        if dur < MIN_DURATION:
            self._upd(jid, id=jid, session=sid, preset=preset, label=name,
                      status="erro", pct=0, opts=o, canceled=False,
                      start=start, seconds=0, available=disponivel,
                      started=time.time(), elapsed=0.0, estimate_mb=0,
                      summary=describe(o),
                      error=("Marque um trecho antes de exportar "
                             f"(mínimo {MIN_DURATION:g} s)."))
            return jid
        dur = min(dur, disponivel - start)

        self._upd(jid, id=jid, session=sid, preset=preset, label=name,
                  status="na fila", pct=0, opts=o, canceled=False,
                  start=start, seconds=dur, available=disponivel,
                  started=time.time(), elapsed=0.0, eta=None, bytes_now=0,
                  estimate_mb=round(estimate_mb(o, dur, self._src_mb_s(sid, start, dur)), 1),
                  summary=describe(o))
        threading.Thread(target=self._run, daemon=True,
                         args=(jid, sid, start, dur, name, o)).start()
        return jid

    def _session_label(self, sid: str) -> str:
        """Nome do jogo como ultimo recurso, no lugar do id tecnico da sessao."""
        m = re.match(r"bg_(\d+)_", sid)
        if m:
            from .steam import game_name
            nome = safe_name(game_name(self.cfg, int(m.group(1))), "")
            if nome:
                return nome
        return "clipe"

    def _src_mb_s(self, sid: str, start: float, dur: float) -> float | None:
        """MB/s reais dos segmentos do trecho - proxy da complexidade da cena.

        A estimativa antiga usava uma media por resolucao e errava por ordens de
        grandeza em menu ou tela de carregamento (ate +13000% na auditoria). O
        tamanho dos proprios segmentos ja diz se a cena e movimentada.
        """
        try:
            seg = Config.SEGMENT_SECONDS
            ch = chunk_list(self.cfg.video_dir / sid, 0)
            i0 = max(0, int(start // seg))
            i1 = min(len(ch), int(-(-(start + dur) // seg)) + 1)
            sel = ch[i0:i1]
            if not sel:
                return None
            return sum(p.stat().st_size for p in sel) / len(sel) / seg / 1048576
        except OSError:
            return None

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
                      error=f"Não há vídeo em {timecode(start, False)}. "
                            f"Esta gravação vai até {timecode(len(vch) * seg, False)}.")
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
            # start e dur ja vieram prendidos ao material em submit(), entao o
            # sufixo descreve o trecho que realmente sai.
            fname = output_name(name, start, dur, ext,
                                with_suffix=bool(opts.get("suffix", True)),
                                fallback=self._session_label(sid))
            out = outdir / fname
            i = 2
            while out.exists():
                stem = Path(fname).stem
                out = outdir / f"{stem} ({i}).{ext}"
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
