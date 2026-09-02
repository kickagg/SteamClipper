# -*- coding: utf-8 -*-
"""Nucleo compartilhado pelas versoes Browser e Desktop do SteamClipper."""

from .config import Config
from .export import (AUDIO_CHOICES, CODECS, CONTAINERS, FPS_CHOICES,
                     PRESETS, SCALES,
                     Jobs, estimate_mb, preset_opts)
from .media import strip, thumb, virtual
from .player import Mpv
from . import waveform
from .steam import chunk_list, game_name, scan_sessions

__version__ = "0.4.0"

__all__ = ["Config", "Jobs", "PRESETS", "Mpv", "CODECS", "CONTAINERS",
           "AUDIO_CHOICES",
           "SCALES", "FPS_CHOICES", "preset_opts", "estimate_mb",
           "scan_sessions", "game_name", "chunk_list",
           "thumb", "strip", "virtual", "waveform", "__version__"]
