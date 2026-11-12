"""Mesures FFmpeg sur une source : changements de plan et silences, en une seule lecture."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .media import MediaError, require_tools
from .selection import Signals

SCENE_THRESHOLD = 0.30
SILENCE_DB = -35
SILENCE_MIN_S = 0.5

_PTS = re.compile(r"pts_time:([0-9.]+)")
_SILENCE_START = re.compile(r"silence_start: (-?[0-9.]+)")
_SILENCE_END = re.compile(r"silence_end: ([0-9.]+)")


def parse_log(text: str, duration_ms: int, has_audio: bool) -> Signals:
    cuts, silences, open_start = [], [], None
    for line in text.splitlines():
        if "showinfo" in line:
            match = _PTS.search(line)
            if match:
                cuts.append(round(float(match.group(1)) * 1000))
        elif "silencedetect" in line:
            start, end = _SILENCE_START.search(line), _SILENCE_END.search(line)
            if start:
                open_start = max(round(float(start.group(1)) * 1000), 0)
            elif end and open_start is not None:
                silences.append((open_start, min(round(float(end.group(1)) * 1000), duration_ms)))
                open_start = None
    if open_start is not None and open_start < duration_ms:
        silences.append((open_start, duration_ms))
    return Signals(duration_ms, sorted(set(cuts)), silences, has_audio)


def analyze(path: Path, info: dict) -> Signals:
    """`info` vient de media.probe()."""
    require_tools()
    graph = f"[0:v:0]scale=320:-2,select='gt(scene,{SCENE_THRESHOLD})',showinfo[v]"
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-loglevel", "info", "-i", str(path)]
    if info["has_audio"]:
        graph += f";[0:a:0]silencedetect=noise={SILENCE_DB}dB:d={SILENCE_MIN_S}[a]"
        command += ["-filter_complex", graph, "-map", "[v]", "-f", "null", "-", "-map", "[a]", "-f", "null", "-"]
    else:
        command += ["-filter_complex", graph, "-map", "[v]", "-f", "null", "-"]
    result = subprocess.run(command, capture_output=True, text=True, errors="replace")
    if result.returncode != 0:
        raise MediaError("Analyse FFmpeg impossible :\n" + result.stderr.strip()[-800:])
    return parse_log(result.stderr, info["duration_ms"], info["has_audio"])
