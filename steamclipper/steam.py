# -*- coding: utf-8 -*-
"""Leitura das gravacoes: nomes dos jogos, sessoes e marcadores."""

from __future__ import annotations

import json
import re
import struct
import threading
from datetime import datetime
from pathlib import Path

from .config import Config, decode, steam_libraries

_names: dict[int, str] = {}
_names_lock = threading.Lock()


# ------------------------------------------------------------ nomes dos jogos
def _from_manifests(cfg: Config) -> dict[int, str]:
    """appmanifest_*.acf cobre os jogos instalados. Barato e sempre atual."""
    out = {}
    if not cfg.steam:
        return out
    for lib in steam_libraries(cfg.steam):
        for acf in (lib / "steamapps").glob("appmanifest_*.acf"):
            try:
                txt = decode(acf.read_bytes())
            except OSError:
                continue
            aid = re.search(r'"appid"\s+"(\d+)"', txt)
            nam = re.search(r'"name"\s+"([^"]+)"', txt)
            if aid and nam:
                out[int(aid.group(1))] = nam.group(1)
    return out


def _from_appinfo(cfg: Config) -> dict[int, str]:
    """appinfo.vdf cobre TODOS os apps conhecidos, inclusive desinstalados.

    Formato binario: header, depois blocos {appid u32, size u32, corpo}. No v29 as
    chaves do KV sao indices numa string table no fim do arquivo; no v28 sao strings
    literais. Em ambos o nome aparece como <0x01> "name" <valor NUL>.
    """
    out: dict[int, str] = {}
    if not cfg.steam:
        return out
    try:
        data = (cfg.steam / "appcache" / "appinfo.vdf").read_bytes()
    except OSError:
        return out

    try:
        magic, _universe = struct.unpack_from("<II", data, 0)
    except struct.error:
        return out

    key_name = None
    if magic == 0x07564429:                       # v29: string table no fim
        try:
            (tbl,) = struct.unpack_from("<q", data, 8)
            off = 16
            n, = struct.unpack_from("<I", data, tbl)
            pos, strs = tbl + 4, []
            for _ in range(n):
                e = data.index(b"\x00", pos)
                strs.append(decode(data[pos:e]))
                pos = e + 1
            key_name = b"\x01" + struct.pack("<I", strs.index("name"))
        except (ValueError, struct.error):
            off, key_name = 16, None
    elif magic == 0x07564428:                     # v28: chaves literais
        off, key_name = 8, b"\x01name\x00"
    else:
        return out

    literal = b"\x01name\x00"
    while off < len(data) - 8:
        try:
            appid, size = struct.unpack_from("<II", data, off)
        except struct.error:
            break
        if appid == 0:
            break
        body, end = off + 8, off + 8 + size
        if end > len(data):
            break
        chunk = data[body:end]
        best = None
        for key in filter(None, (literal, key_name)):
            i = chunk.find(key)
            if i >= 0 and (best is None or i < best[0]):
                best = (i, i + len(key))
        if best:
            try:
                j = chunk.index(b"\x00", best[1])
                name = decode(chunk[best[1]:j]).strip()
                if name:
                    out[appid] = name
            except ValueError:
                pass
        off = end
    return out


def game_name(cfg: Config, appid: int) -> str:
    global _names
    with _names_lock:
        if not _names:
            _names = _from_appinfo(cfg)
            _names.update(_from_manifests(cfg))    # instalado tem prioridade
    return _names.get(appid) or f"App {appid}"


# ------------------------------------------------------------------- sessoes
def parse_stamp(date_s: str, time_s: str):
    try:
        return datetime.strptime(date_s + time_s, "%Y%m%d%H%M%S")
    except ValueError:
        return None


def chunk_list(session_dir: Path, stream: int) -> list[Path]:
    return sorted(session_dir.glob(f"chunk-stream{stream}-*.m4s"))


def _markers(cfg: Config, appid: int, started: datetime | None) -> list[dict]:
    """Casa o timeline_*.json da sessao pelo appid e pelo carimbo mais proximo.

    O nome do timeline usa um horario alguns segundos diferente do bg_
    correspondente, entao o pareamento e por proximidade, nao por igualdade.
    """
    if not started or not cfg.recordings:
        return []
    best, best_d = None, 600
    for f in (cfg.recordings / "timelines").glob(f"timeline_{appid}*.json"):
        m = re.match(rf"timeline_{appid}(\d{{8}})_(\d{{6}})\.json$", f.name)
        if not m:
            continue
        ts = parse_stamp(m.group(1), m.group(2))
        if not ts:
            continue
        d = abs((ts - started).total_seconds())
        if d < best_d:
            best, best_d = f, d
    if not best:
        return []
    try:
        data = json.loads(best.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for e in data.get("entries", []):
        try:
            out.append({"t": int(e["time"]) / 1e6, "type": e.get("type", "marker")})
        except (KeyError, ValueError, TypeError):
            pass
    return out


def scan_sessions(cfg: Config) -> list[dict]:
    """Lista as gravacoes disponiveis, com o que sobrou do buffer circular."""
    out = []
    root = cfg.video_dir
    if not root or not root.is_dir():
        return out

    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        video = chunk_list(d, 0)
        if not video:
            continue

        m = re.match(r"bg_(\d+)_(\d{8})_(\d{6})$", d.name)
        appid = int(m.group(1)) if m else 0
        started = parse_stamp(m.group(2), m.group(3)) if m else None
        first = int(video[0].stem.split("-")[-1])

        w = h = 0
        mpd = d / "session.mpd"
        if mpd.is_file():
            try:
                r = re.search(r'width="(\d+)" height="(\d+)"',
                              mpd.read_text("utf-8", "ignore"))
                if r:
                    w, h = int(r.group(1)), int(r.group(2))
            except OSError:
                pass

        size = sum(p.stat().st_size for p in d.glob("*.m4s"))
        secs = len(video) * Config.SEGMENT_SECONDS
        out.append({
            "id": d.name,
            "appid": appid,
            "game": game_name(cfg, appid) if appid else d.name,
            "started": started.isoformat() if started else None,
            "started_h": started.strftime("%d/%m/%Y %H:%M") if started else "",
            "seconds": secs,
            "chunks": len(video),
            "width": w, "height": h,
            "bytes": size,
            "gb_h": round(size / 1073741824 / (secs / 3600), 1) if secs else 0,
            # O buffer circular apaga os segmentos antigos mas nao atualiza o
            # session.mpd, que continua dizendo startNumber="1".
            "pruned": first > 1,
            "pruned_min": round((first - 1) * Config.SEGMENT_SECONDS / 60) if first > 1 else 0,
            "has_audio": bool(chunk_list(d, 1)),
            "markers": _markers(cfg, appid, started),
        })
    return out
