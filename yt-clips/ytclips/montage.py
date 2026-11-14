"""Montage de 60 secondes : choix de 4 à 6 passages d'un film. Fonctions pures.

Le programme ne comprend pas l'histoire. Il s'appuie sur ce qui est mesurable :
un passage par partie du film, dans l'ordre chronologique, des phrases entières,
rien après une limite fixée avant la fin du film, pas de répétition.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .media import MediaError, _srt_stamp, format_timecode
from .selection import PAUSE_MS, Bounds, Cue, Signals, _safe, _silence_stats

TARGET_MS = 60_000
MIN_SEGMENTS, MAX_SEGMENTS = 4, 6
SEGMENT_MIN_MS, SEGMENT_MAX_MS = 4_000, 20_000
INNER_GAP_MS = 2_500      # pause maximale entre deux répliques d'un même passage
LEAD_MS, TAIL_MS = 150, 250  # respiration minimale avant et après le dialogue
PAD_MAX_MS = 1_500
SPOILER_CUTOFF = 0.75     # rien n'est pris après cette fraction du film
TOP_PER_ZONE = 10
SIMILARITY_MAX = 0.5


@dataclass
class Piece:
    core_start: int
    core_end: int
    lead: int  # marge disponible avant, sans toucher une autre réplique ni un autre plan
    tail: int
    score: float
    text: str
    cues: int
    cuts: int
    density: float
    words: frozenset = frozenset()

    @property
    def low(self) -> int:
        return self.core_end - self.core_start + min(self.lead, LEAD_MS) + min(self.tail, TAIL_MS)

    @property
    def high(self) -> int:
        return self.core_end - self.core_start + self.lead + self.tail


@dataclass
class Segment:
    role: str
    start_ms: int
    end_ms: int
    text: str
    reason: str
    score: float


@dataclass
class Plan:
    segments: list[Segment]
    total_ms: int
    limit_ms: int
    title: str
    description: str
    whole_sentences: bool


def _words(text: str) -> set[str]:
    return {word for word in re.findall(r"\w+", text.lower()) if len(word) > 3}


def _alike(left: frozenset, right: frozenset) -> float:
    return len(left & right) / len(left | right) if left and right else 0.0


def similarity(a: str, b: str) -> float:
    return _alike(frozenset(_words(a)), frozenset(_words(b)))


def _shortlist(zone: list[Piece]) -> list[Piece]:
    """Meilleurs passages d'une zone, de durées variées : sans ce choix, tomber sur 60 s exactes est rare."""
    kept, per_length = [], {}
    for piece in sorted(zone, key=lambda p: (-p.score, p.core_start)):
        length = (piece.core_end - piece.core_start) // 1000
        if per_length.get(length, 0) < 2:
            per_length[length] = per_length.get(length, 0) + 1
            kept.append(piece)
        if len(kept) == TOP_PER_ZONE:
            break
    return kept


def _pieces(
    signals: Signals, cues: list[Cue], low_limit: int, high_limit: int, strict: bool, keep_shots: bool,
) -> list[Piece]:
    bounds = Bounds(cues, signals.duration_ms)
    cuts = signals.scene_cuts
    pieces = []
    for i in range(len(cues)):
        if cues[i].start < low_limit or not bounds.start_ok(i, strict):
            continue
        for j in range(i, len(cues)):
            if cues[j].music or (j > i and cues[j].start - cues[j - 1].end > INNER_GAP_MS):
                break
            start, end = cues[i].start, cues[j].end
            if end - start > SEGMENT_MAX_MS or end > high_limit:
                break
            if end - start < SEGMENT_MIN_MS or not bounds.end_ok(j, strict):
                continue
            if _silence_stats(signals, start, end)[1] > INNER_GAP_MS:
                continue
            lead = max(min(bounds.gap_before(i), PAD_MAX_MS, start - low_limit), 0)
            tail = max(min(bounds.gap_after(j), PAD_MAX_MS, high_limit - end), 0)
            # Une marge ne traverse pas un changement de plan : pas d'image fugitive d'un autre plan.
            before = [cut for cut in cuts if start - lead < cut <= start]
            after = [cut for cut in cuts if end <= cut < end + tail]
            if before and keep_shots:
                lead = start - max(before)
            if after and keep_shots:
                tail = min(after) - end
            inside = cues[i : j + 1]
            text = " ".join(cue.text for cue in inside)
            density = sum(cue.end - cue.start for cue in inside) / (end - start)
            inner = sum(1 for cut in cuts if start < cut < end)
            calm = bounds.gap_before(i) >= PAUSE_MS and bounds.gap_after(j) >= PAUSE_MS
            score = (
                0.45 * min(density / 0.85, 1)
                + 0.25 * min((text.count("?") + text.count("!")) / 2, 1)
                + 0.15 * min(inner / ((end - start) / 6000), 1)
                + 0.15 * (1 if calm else 0.5)
            )
            pieces.append(
                Piece(start, end, lead, tail, round(score, 4), text, len(inside), inner, density, frozenset(_words(text)))
            )
    return pieces


def _search(zones: list[list[Piece]], target: int) -> tuple[list[Piece] | None, float]:
    """Meilleure combinaison d'un passage par zone dont la durée peut atteindre exactement la cible."""
    count = len(zones)
    rest_low, rest_high = [0] * (count + 1), [0] * (count + 1)
    for k in range(count - 1, -1, -1):
        rest_low[k] = rest_low[k + 1] + min(piece.low for piece in zones[k])
        rest_high[k] = rest_high[k + 1] + max(piece.high for piece in zones[k])
    best: tuple[list[Piece] | None, float] = (None, -1.0)
    chosen: list[Piece] = []

    def walk(k: int, low: int, high: int, score: float) -> None:
        nonlocal best
        if k == count:
            if low <= target <= high and score > best[1]:
                best = (list(chosen), score)
            return
        for piece in zones[k]:
            if chosen and piece.core_start - piece.lead < chosen[-1].core_end + chosen[-1].tail:
                continue
            if any(_alike(piece.words, other.words) > SIMILARITY_MAX for other in chosen):
                continue
            new_low, new_high = low + piece.low, high + piece.high
            if new_low + rest_low[k + 1] > target or new_high + rest_high[k + 1] < target:
                continue
            bonus = 0.1 if k == count - 1 and piece.text.rstrip(" \"'»”’)").endswith(("?", "!", "…", "...")) else 0
            chosen.append(piece)
            walk(k + 1, new_low, new_high, score + piece.score + bonus)
            chosen.pop()

    walk(0, 0, 0, 0.0)
    return best


def _fit(chosen: list[Piece], target: int) -> list[tuple[int, int]]:
    """Répartit les marges pour que la somme des passages vaille exactement la cible."""
    pads = [[min(piece.lead, LEAD_MS), min(piece.tail, TAIL_MS)] for piece in chosen]
    slots = [[n, side, room] for n, piece in enumerate(chosen)
             for side, room in ((0, piece.lead - pads[n][0]), (1, piece.tail - pads[n][1])) if room > 0]
    remaining = target - sum(piece.low for piece in chosen)
    while remaining > 0:
        share = max(remaining // len(slots), 1)
        for slot in list(slots):
            give = min(share, slot[2], remaining)
            pads[slot[0]][slot[1]] += give
            slot[2] -= give
            remaining -= give
            if slot[2] == 0:
                slots.remove(slot)
            if remaining == 0:
                break
    return [(piece.core_start - pads[n][0], piece.core_end + pads[n][1]) for n, piece in enumerate(chosen)]


def _role(index: int, count: int) -> str:
    return "Introduction" if index == 0 else "Conclusion" if index == count - 1 else "Progression"


def _texts(title: str, ranges: list[tuple[int, int]], total_ms: int) -> tuple[str, str]:
    marks = ", ".join(format_timecode(start)[:8] for start, _ in ranges)
    seconds = round(total_ms / 1000)
    return (
        _safe(f"{title} — en {seconds} secondes", 100),
        f"Montage de {len(ranges)} passages de « {_safe(title, 80)} », dans l'ordre du film : {marks}.",
    )


def plan(
    signals: Signals, cues: list[Cue], title: str, *, target_ms: int = TARGET_MS,
    cutoff: float = SPOILER_CUTOFF, skip_start_ms: int = 0, skip_end_ms: int = 0,
) -> Plan | None:
    """4 à 6 passages, un par partie du film, totalisant exactement `target_ms`. None si impossible."""
    duration = signals.duration_ms
    cues = [cue for cue in cues if cue.end <= duration + 500]
    low_limit = skip_start_ms
    high_limit = min(int(duration * cutoff), duration - skip_end_ms)
    # Du plus exigeant au moins exigeant : phrases entières avant tout, puis marges cantonnées à leur plan.
    for strict, keep_shots in ((True, True), (True, False), (False, True), (False, False)):
        pieces = _pieces(signals, cues, low_limit, high_limit, strict, keep_shots)
        best: tuple[list[Piece] | None, float] = (None, -1.0)
        for count in (5, 4, 6):
            span = (high_limit - low_limit) / count
            zones = [[] for _ in range(count)]
            for piece in pieces:
                middle = (piece.core_start + piece.core_end) / 2
                zones[min(int((middle - low_limit) / span), count - 1)].append(piece)
            if any(not zone for zone in zones):
                continue
            chosen, score = _search([_shortlist(zone) for zone in zones], target_ms)
            if chosen and score / count > best[1]:
                best = (chosen, score / count)
        if best[0]:
            chosen = best[0]
            ranges = _fit(chosen, target_ms)
            sentences = (
                "commence et finit sur une phrase entière" if strict
                else "bornes posées sur des pauses, la ponctuation des sous-titres ne permettant pas mieux"
            )
            if not keep_shots:
                sentences += " ; la marge peut déborder d'un instant sur le plan voisin pour atteindre la durée exacte"
            segments = []
            for n, (piece, (start, end)) in enumerate(zip(chosen, ranges)):
                role = _role(n, len(chosen))
                reason = (
                    f"{role} : {piece.cues} réplique(s) à {format_timecode(piece.core_start)[:8]}, "
                    f"soit à {piece.core_start / duration:.0%} du film ; {sentences} ; "
                    f"dialogue sur {piece.density:.0%} du passage ; {piece.cuts} changement(s) de plan."
                )
                segments.append(Segment(role, start, end, piece.text, reason, piece.score))
            return Plan(segments, target_ms, high_limit, *_texts(title, ranges, target_ms), whole_sentences=strict)
    return None


def manual(
    ranges: list[tuple[int, int]], signals: Signals, cues: list[Cue], title: str, target_ms: int = TARGET_MS,
) -> Plan:
    """Plan imposé à la main : mêmes règles de nombre, d'ordre, de durée et de phrases entières."""
    if not MIN_SEGMENTS <= len(ranges) <= MAX_SEGMENTS:
        raise MediaError(f"Un montage compte de {MIN_SEGMENTS} à {MAX_SEGMENTS} passages ({len(ranges)} fournis).")
    previous = 0
    for start, end in ranges:
        if not previous <= start < end <= signals.duration_ms:
            raise MediaError("Les passages doivent être dans l'ordre du film, sans recouvrement, et dans sa durée.")
        previous = end
        for cue in cues:
            if not cue.music and (cue.start < start < cue.end or cue.start < end < cue.end):
                raise MediaError(
                    f"Le passage {format_timecode(start)} → {format_timecode(end)} coupe la réplique « {_safe(cue.text, 60)} »."
                )
    total = sum(end - start for start, end in ranges)
    if total != target_ms:
        raise MediaError(f"Les passages totalisent {total / 1000:.3f} s : il faut exactement {target_ms / 1000:.0f} s.")
    segments = []
    for n, (start, end) in enumerate(ranges):
        text = " ".join(cue.text for cue in cues if not cue.music and cue.start >= start and cue.end <= end)
        segments.append(Segment(_role(n, len(ranges)), start, end, text, "Choisi à la main.", 0.0))
    return Plan(segments, target_ms, signals.duration_ms, *_texts(title, ranges, target_ms), whole_sentences=True)


def compose_srt(cues: list[Cue], ranges: list[tuple[int, int]]) -> str:
    """Sous-titres recalés sur la ligne de temps du montage."""
    blocks, offset = [], 0
    for start, end in ranges:
        for cue in cues:
            if cue.music:
                continue
            shown_start, shown_end = max(cue.start, start), min(cue.end, end)
            if shown_end - shown_start >= 200:
                blocks.append(
                    f"{len(blocks) + 1}\n{_srt_stamp(shown_start - start + offset)} --> "
                    f"{_srt_stamp(shown_end - start + offset)}\n{cue.text}\n\n"
                )
        offset += end - start
    return "".join(blocks)
