# -*- coding: utf-8 -*-
"""Nucleo compartilhado pelas versoes Browser e Desktop do SteamClipper."""

from .config import Config
from .export import PRESETS, Jobs
from .media import strip, thumb, virtual
from .player import Mpv
from .steam import chunk_list, game_name, scan_sessions

__version__ = "0.2.0"

__all__ = ["Config", "Jobs", "PRESETS", "Mpv",
           "scan_sessions", "game_name", "chunk_list",
           "thumb", "strip", "virtual", "__version__"]
