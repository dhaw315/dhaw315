"""Choix automatique d'extraits : dialogues (SRT), changements de plan et silences.

Fonctions pures, sans FFmpeg ni réseau. Le score mesure la tenue d'un passage
(dialogue continu, bornes propres) ; il ne prédit pas son audience.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import MAX_CLIP_MS, MIN_CLIP_MS
from .media import format_timecode

MAX_PROPOSALS = 3
PAUSE_MS = 1200          # pause qui sépare nettement deux prises de parole
MAX_GAP_MS = 4000        # au-delà, sans dialogue, l'extrait est coupé (générique, plan muet)
SNAP_MS = 1500           # distance maximale pour caler une borne sur un changement de plan
MAX_INNER_SILENCE_MS = 4000
MIN_SPEECH_RATIO = 0.35
USED_OVERLAP_MS = 3000   # recouvrement toléré avec un passage déjà utilisé


@dataclass(frozen=True)
class Cue:
    start: int
    end: int
    text: str
    music: bool = False


@dataclass
class Signals:
    duration_ms: int
    scene_cuts: list[int] = field(default_factory=list)
    silences: list[tuple[int, int]] = field(default_factory=list)
    has_audio: bool = True


@dataclass
class Candidate:
    start_ms: int
    end_ms: int
    score: float
    basis: str  # « srt » ou « audio »
    reason: str
    title: str
    description: str
    metrics: dict


_TAGS = re.compile(r"<[^>]+>|\{\\[^}]*\}")
_NON_SPEECH = re.compile(r"^[\[(（].*[\])）]$")


def clean_text(text: str) -> str:
    lines = [re.sub(r"^\s*[-–—]\s*", "", line).strip() for line in _TAGS.sub("", text).splitlines()]
    return " ".join(line for line in lines if line)


def is_music(text: str) -> bool:
    """Paroles de chanson, bruitages et indications entre crochets : pas du dialogue."""
    cleaned = clean_text(text)
    return (
        not cleaned or "♪" in cleaned or "♫" in cleaned or bool(_NON_SPEECH.match(cleaned))
        or (len(cleaned) > 1 and cleaned.startswith("#") and cleaned.endswith("#"))
    )


def make_cues(raw: list[tuple[int, int, str]]) -> list[Cue]:
    return [Cue(start, end, clean_text(text), is_music(text)) for start, end, text in sorted(raw)]


def ends_sentence(text: str) -> bool:
    stripped = text.rstrip(" \"'»”’)")
    if stripped.endswith(("...", "…")):
        return False
    return stripped.endswith((".", "!", "?"))


class Bounds:
    """Où un extrait peut commencer et finir sans couper une phrase.

    En mode strict, une pause ne suffit pas : la réplique doit se terminer par une
    ponctuation de fin de phrase ou des points de suspension.
    """

    def __init__(self, cues: list[Cue], duration_ms: int):
        self.cues, self.duration_ms = cues, duration_ms

    def gap_before(self, i: int) -> int:
        return self.cues[i].start - self.cues[i - 1].end if i > 0 else self.cues[i].start

    def gap_after(self, i: int) -> int:
        if i + 1 < len(self.cues):
            return self.cues[i + 1].start - self.cues[i].end
        return self.duration_ms - self.cues[i].end

    def end_ok(self, i: int, strict: bool = False) -> bool:
        text = self.cues[i].text
        if ends_sentence(text):
            return True
        if strict:
            return text.rstrip(" \"'»”’)").endswith(("...", "…"))
        return self.gap_after(i) >= PAUSE_MS or i + 1 == len(self.cues) or self.cues[i + 1].music

    def start_ok(self, i: int, strict: bool = False) -> bool:
        if self.cues[i].music:
            return False
        if i == 0 or self.cues[i - 1].music:
            return True
        if self.cues[i].text[:1].islower() and (strict or self.gap_before(i) < PAUSE_MS):
            return False  # suite d'une phrase commencée avant
        return self.end_ok(i - 1, strict)


def _overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start))


def _silence_stats(signals: Signals, start: int, end: int) -> tuple[int, int]:
    parts = [_overlap(start, end, s, e) for s, e in signals.silences]
    return sum(parts), max(parts, default=0)


def _seconds(ms: int) -> str:
    return f"{ms / 1000:.1f}".replace(".", ",") + " s"


def _span(start: int, end: int) -> str:
    return f"de {format_timecode(start)[3:8]} à {format_timecode(end)[3:8]}"


def _safe(text: str, limit: int) -> str:
    text = text.replace("<", "").replace(">", "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"


def _dialogue_candidates(signals: Signals, cues: list[Cue], title: str, skip_start: int, skip_end: int) -> list[Candidate]:
    count = len(cues)
    limit_end = signals.duration_ms - skip_end
    bounds = Bounds(cues, signals.duration_ms)
    gap_before, gap_after, end_ok, start_ok = bounds.gap_before, bounds.gap_after, bounds.end_ok, bounds.start_ok

    candidates = []
    for i in range(count):
        if not start_ok(i):
            continue
        for j in range(i, count):
            if cues[j].music or (j > i and cues[j].start - cues[j - 1].end > MAX_GAP_MS):
                break
            if cues[j].end - cues[i].start > MAX_CLIP_MS:
                break
            if not end_ok(j):
                continue

            # Bornes : jamais dans une réplique voisine, calées sur un changement de plan s'il est proche.
            low = max(cues[i - 1].end if i > 0 else 0, skip_start, cues[i].start - SNAP_MS)
            high = min(cues[j + 1].start if j + 1 < count else signals.duration_ms, limit_end, cues[j].end + SNAP_MS)
            if low > cues[i].start or high < cues[j].end:
                continue  # empiète sur une zone exclue
            cuts_before = [cut for cut in signals.scene_cuts if low <= cut <= cues[i].start]
            cuts_after = [cut for cut in signals.scene_cuts if cues[j].end + 100 <= cut <= high]
            start = max(cuts_before) if cuts_before else max(low, cues[i].start - 300)
            end = min(cuts_after) if cuts_after else min(high, cues[j].end + 400)
            if end - start > MAX_CLIP_MS:
                start, end = max(low, cues[i].start - 300), min(high, cues[j].end + 400)
                cuts_before, cuts_after = [], []
            duration = end - start
            if not MIN_CLIP_MS <= duration <= MAX_CLIP_MS:
                continue

            inside = cues[i : j + 1]
            speech_ratio = sum(cue.end - cue.start for cue in inside) / duration
            silence_ms, longest = _silence_stats(signals, start, end)
            if speech_ratio < MIN_SPEECH_RATIO or longest > MAX_INNER_SILENCE_MS:
                continue
            inner_cuts = sum(1 for cut in signals.scene_cuts if start < cut < end)
            text = " ".join(cue.text for cue in inside)
            marks = text.count("?") + text.count("!")
            clean_start = bool(cuts_before) or gap_before(i) >= PAUSE_MS
            clean_end = bool(cuts_after) or gap_after(j) >= PAUSE_MS
            score = (
                0.40 * min(speech_ratio / 0.8, 1)
                + 0.15 * (1 - min(silence_ms / duration / 0.3, 1))
                + 0.10 * (1 if clean_start else 0.5)
                + 0.10 * (1 if clean_end else 0.5)
                + 0.10 * min(inner_cuts / (duration / 10000), 1)
                + 0.10 * min(marks / 3, 1)
                + 0.05 * (1 if 25000 <= duration <= 50000 else 0.5)
            )

            begin = "commence au début d'une phrase"
            if cuts_before:
                begin += ", sur un changement de plan"
            elif gap_before(i) >= PAUSE_MS:
                begin += f", après une pause de {_seconds(gap_before(i))}"
            finish = "se termine sur une fin de phrase"
            if cuts_after:
                finish += ", sur un changement de plan"
            elif gap_after(j) >= PAUSE_MS:
                finish += f", avant une pause de {_seconds(gap_after(j))}"
            if not signals.has_audio:
                quiet = "piste audio absente"
            elif longest < 1000:
                quiet = "aucun silence de plus d'une seconde"
            else:
                quiet = f"plus long silence : {_seconds(longest)}"
            reason = (
                f"{len(inside)} réplique(s), dialogue sur {speech_ratio:.0%} de la durée ; {begin} ; {finish} ; "
                f"{inner_cuts} changement(s) de plan à l'intérieur ; {quiet}."
            )

            hook = next((cue.text for cue in inside if 15 <= len(cue.text) <= 80 and cue.text.rstrip()[-1:] in "?!"), None)
            first = re.split(r"(?<=[.!?])\s", inside[0].text, maxsplit=1)[0]
            candidates.append(Candidate(
                start, end, round(score, 4), "srt", reason,
                _safe(hook or first, 80) or title,
                _safe(text, 220) + f"\n\nExtrait de « {_safe(title, 80)} », {_span(start, end)}.",
                {
                    "cues": len(inside), "speech_ratio": round(speech_ratio, 3), "silence_ms": silence_ms,
                    "longest_silence_ms": longest, "scene_cuts": inner_cuts,
                    "start_on_cut": bool(cuts_before), "end_on_cut": bool(cuts_after),
                },
            ))
    return candidates


def _audio_candidates(signals: Signals, title: str, skip_start: int, skip_end: int) -> list[Candidate]:
    """Sans sous-titres : les seules bornes défendables sont les pauses de la bande-son."""
    if not signals.has_audio or not signals.silences:
        return []
    duration = signals.duration_ms
    silences = sorted(signals.silences)
    starts = [(end, end - start) for start, end in silences if end < duration]
    ends = [(start, end - start) for start, end in silences if start > 0]
    if silences[0][0] > 0:
        starts.insert(0, (0, PAUSE_MS))
    if silences[-1][1] < duration:
        ends.append((duration, PAUSE_MS))

    candidates = []
    for sound_start, pause_before in starts:
        for sound_end, pause_after in ends:
            start, end = max(sound_start - 150, 0), min(sound_end + 250, duration)
            length = end - start
            if length > MAX_CLIP_MS:
                break
            if length < MIN_CLIP_MS or start < skip_start or end > duration - skip_end:
                continue
            silence_ms, longest = _silence_stats(signals, start + 150, end - 250)
            if longest > MAX_INNER_SILENCE_MS:
                continue
            inner_cuts = sum(1 for cut in signals.scene_cuts if start < cut < end)
            score = (
                0.50 * (1 - min(silence_ms / length / 0.3, 1))
                + 0.20 * min(pause_before / 2000, 1)
                + 0.20 * min(pause_after / 2000, 1)
                + 0.10 * min(inner_cuts / (length / 10000), 1)
            )
            reason = (
                "Sans sous-titres : bornes posées sur des pauses de la bande-son "
                f"({_seconds(pause_before)} avant, {_seconds(pause_after)} après), les fins de phrases ne sont pas vérifiées ; "
                f"{inner_cuts} changement(s) de plan à l'intérieur ; silences cumulés : {_seconds(silence_ms)}."
            )
            candidates.append(Candidate(
                start, end, round(score, 4), "audio", reason, "",
                f"Extrait de « {_safe(title, 80)} », {_span(start, end)}.",
                {"silence_ms": silence_ms, "longest_silence_ms": longest, "scene_cuts": inner_cuts},
            ))
    return candidates


def select(
    signals: Signals, cues: list[Cue], title: str, used: list[tuple[int, int]] | None = None,
    max_count: int = MAX_PROPOSALS, skip_start_ms: int = 0, skip_end_ms: int = 0,
) -> list[Candidate]:
    """Au plus `max_count` extraits distincts de 20 à 60 s, hors passages déjà utilisés."""
    used = used or []
    cues = [cue for cue in cues if cue.end <= signals.duration_ms + 500]
    if any(not cue.music for cue in cues):
        candidates = _dialogue_candidates(signals, cues, title, skip_start_ms, skip_end_ms)
    else:
        candidates = _audio_candidates(signals, title, skip_start_ms, skip_end_ms)

    chosen: list[Candidate] = []
    for candidate in sorted(candidates, key=lambda c: (-c.score, c.start_ms, c.end_ms)):
        if any(_overlap(candidate.start_ms, candidate.end_ms, s, e) > USED_OVERLAP_MS for s, e in used):
            continue
        if any(_overlap(candidate.start_ms, candidate.end_ms, o.start_ms, o.end_ms) > 0 for o in chosen):
            continue
        chosen.append(candidate)
        if len(chosen) == max_count:
            break
    chosen.sort(key=lambda c: c.start_ms)
    for candidate in chosen:
        if not candidate.title:
            candidate.title = _safe(f"{title} — extrait {_span(candidate.start_ms, candidate.end_ms)}", 100)
    return chosen
