import email.message
import io
import urllib.error

import pytest

from ytclips import analysis, config, importer, media, selection, storage
from ytclips.selection import Cue, Signals


def cues_from(rows):
    return selection.make_cues([(round(s * 1000), round(e * 1000), t) for s, e, t in rows])


SCENE = [
    (0.0, 5.0, "Bonjour à tous."),
    (5.5, 10.0, "Je voulais vous dire,"),
    (10.5, 15.0, "que tout va bien."),
    (15.5, 20.0, "Merci d'être venus."),
    (20.5, 25.0, "On commence,"),
    (25.5, 30.0, "dès que possible."),
]


def long_dialogue(minutes=10):
    rows, t, n = [], 2.0, 0
    while t < minutes * 60 - 5:
        n += 1
        rows.append((t, t + 3.0, f"Phrase numéro {n}."))
        t += 3.5 + (2.0 if n % 10 == 0 else 0)
    return rows


# --- texte -------------------------------------------------------------------

def test_text_helpers():
    assert selection.clean_text("<i>- Salut !</i>\n{\\an8}- Ça va ?") == "Salut ! Ça va ?"
    for text in ("♪ La la la ♪", "[Musique]", "(rires)", "# refrain #", "<i></i>"):
        assert selection.is_music(text), text
    assert not selection.is_music("Tu viens (ou pas) ?")
    assert selection.ends_sentence('Il a dit : « Non. »') and selection.ends_sentence("Vraiment ?")
    assert not selection.ends_sentence("Je ne sais pas...") and not selection.ends_sentence("Et alors,")


def test_parse_srt_and_ffmpeg_log():
    cues = media.parse_srt("2\n00:00:05,000 --> 00:00:07,500\nB\n\n1\n00:00:01,000 --> 00:00:02,000\nA\nsuite\n")
    assert cues == [(1000, 2000, "A\nsuite"), (5000, 7500, "B")]
    log = (
        "[Parsed_showinfo_2 @ 0x1] n:   0 pts:  76800 pts_time:30      duration_time:0.2\n"
        "[silencedetect @ 0x2] silence_start: -0.01\n"
        "[silencedetect @ 0x2] silence_end: 2.02 | silence_duration: 2.03\n"
        "[Parsed_showinfo_2 @ 0x1] n:   1 pts: 179200 pts_time:70.4    duration_time:0.2\n"
        "[silencedetect @ 0x2] silence_start: 97\n"
    )
    signals = analysis.parse_log(log, 100_000, True)
    assert signals.scene_cuts == [30_000, 70_400]
    assert signals.silences == [(0, 2020), (97_000, 100_000)]


# --- sélection sur dialogues -------------------------------------------------

def test_never_starts_or_ends_inside_a_sentence():
    candidates = selection._dialogue_candidates(Signals(31_000), cues_from(SCENE), "t", 0, 0)
    assert candidates
    for c in candidates:
        assert c.start_ms in (0, 5200), c  # « Bonjour… » ou « Je voulais… », jamais « que tout va bien »
        assert c.end_ms in (20_400, 30_400), c  # après « venus. » ou « possible. », jamais après une virgule
        assert 20_000 <= c.end_ms - c.start_ms <= 60_000
    picks = selection.select(Signals(31_000), cues_from(SCENE), "Ma vidéo")
    assert len(picks) == 1 and picks[0].basis == "srt"
    assert picks[0].title == "Bonjour à tous." and "Extrait de « Ma vidéo »" in picks[0].description
    assert "début d'une phrase" in picks[0].reason and "fin de phrase" in picks[0].reason


def test_at_most_three_distinct_extracts():
    rows = long_dialogue()
    picks = selection.select(Signals(600_000), cues_from(rows), "t")
    assert len(picks) == 3
    for a, b in zip(picks, picks[1:]):
        assert a.end_ms <= b.start_ms
    assert all(20_000 <= p.end_ms - p.start_ms <= 60_000 for p in picks)
    assert len(selection.select(Signals(600_000), cues_from(rows), "t", max_count=2)) == 2


def test_credits_music_and_long_gaps_are_excluded():
    rows = [(float(t), t + 4.0, "♪ Chanson du générique ♪") for t in range(0, 40, 5)]
    rows += [(45 + 4.0 * i, 48.5 + 4.0 * i, f"Réplique {i}.") for i in range(8)]  # 45 s → 77 s
    rows += [(90.0, 93.0, "Une phrase isolée.")]
    picks = selection.select(Signals(120_000), cues_from(rows), "t")
    assert len(picks) == 1
    assert picks[0].start_ms >= 43_500 and picks[0].end_ms <= 79_000


def test_bounds_snap_to_nearby_scene_cuts_only():
    rows = [(10 + 4.0 * i, 13.5 + 4.0 * i, f"Réplique {i}.") for i in range(7)]  # 10 s → 37,5 s
    signals = Signals(60_000, scene_cuts=[5_000, 9_200, 20_000, 38_300, 45_000])
    best = selection.select(signals, cues_from(rows), "t")[0]
    assert (best.start_ms, best.end_ms) == (9_200, 38_300)
    assert best.metrics["start_on_cut"] and best.metrics["end_on_cut"] and best.metrics["scene_cuts"] == 1
    assert "changement de plan" in best.reason


def test_used_passages_and_skipped_zones():
    rows = long_dialogue(3)
    free = selection.select(Signals(180_000), cues_from(rows), "t")
    assert free
    again = selection.select(Signals(180_000), cues_from(rows), "t", used=[(p.start_ms, p.end_ms) for p in free])
    for new in again:
        for old in free:
            assert min(new.end_ms, old.end_ms) - max(new.start_ms, old.start_ms) <= selection.USED_OVERLAP_MS
    assert selection.select(Signals(180_000), cues_from(rows), "t", used=[(0, 180_000)]) == []
    trimmed = selection.select(Signals(180_000), cues_from(rows), "t", skip_start_ms=60_000, skip_end_ms=30_000)
    assert trimmed and all(p.start_ms >= 60_000 and p.end_ms <= 150_000 for p in trimmed)


def test_long_silences_disqualify_a_passage():
    rows = [(4.0 * i, 3.5 + 4.0 * i, f"Réplique {i}.") for i in range(8)]
    assert selection.select(Signals(40_000), cues_from(rows), "t")
    assert selection.select(Signals(40_000, silences=[(10_000, 16_000)]), cues_from(rows), "t") == []


# --- sélection sans sous-titres ----------------------------------------------

def test_audio_only_fallback_uses_pauses():
    signals = Signals(100_000, scene_cuts=[30_000], silences=[(0, 2_000), (28_000, 33_000), (66_000, 72_000), (97_000, 100_000)])
    picks = selection.select(signals, [], "Épisode")
    assert [p.basis for p in picks] == ["audio"] * 3
    assert [(p.start_ms, p.end_ms) for p in picks] == [(1_850, 28_250), (32_850, 66_250), (71_850, 97_250)]
    assert "ne sont pas vérifiées" in picks[0].reason and picks[0].title.startswith("Épisode — extrait")
    assert selection.select(Signals(100_000), [], "t") == []  # aucune pause : aucune borne défendable
    assert selection.select(Signals(100_000, silences=[(40_000, 41_000)], has_audio=False), [], "t") == []


# --- import ------------------------------------------------------------------

class FakeResponse(io.BytesIO):
    def __init__(self, data, content_type):
        super().__init__(data)
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, 0

    def open(self, request, timeout=None):
        self.calls += 1
        if self.error:
            raise self.error
        return self.response


@pytest.fixture
def bucket(tmp_path, monkeypatch):
    monkeypatch.setenv(config.ENV_STORAGE_DIR, str(tmp_path / "bucket"))
    monkeypatch.delenv(config.ENV_IMPORT_HOSTS, raising=False)
    return storage.from_env()


def test_import_is_disabled_until_hosts_are_listed(bucket, monkeypatch):
    opener = FakeOpener()
    url = "https://media.exemple.org/videos/episode.mp4"
    with pytest.raises(importer.ImportRefused, match="Import désactivé"):
        importer.import_url(bucket, url, rights_confirmed=True, opener=opener)
    monkeypatch.setenv(config.ENV_IMPORT_HOSTS, "media.exemple.org")
    with pytest.raises(importer.ImportRefused, match="rights-confirmed"):
        importer.import_url(bucket, url, rights_confirmed=False, opener=opener)
    for bad, message in [
        ("https://autre-site.com/episode.mp4", "n'est pas dans la liste"),
        ("http://media.exemple.org/episode.mp4", "https"),
        ("https://media.exemple.org/watch?v=123", "directement un fichier vidéo"),
        ("https://user:pw@media.exemple.org/episode.mp4", "identifiants"),
    ]:
        with pytest.raises(importer.ImportRefused, match=message):
            importer.import_url(bucket, bad, rights_confirmed=True, opener=opener)
    assert opener.calls == 0 and bucket.list() == []


def test_import_refuses_pages_denied_access_and_unreadable_files(bucket, monkeypatch):
    monkeypatch.setenv(config.ENV_IMPORT_HOSTS, "media.exemple.org")
    url = "https://media.exemple.org/episode.mp4"
    page = FakeOpener(FakeResponse(b"<html>connexion requise</html>", "text/html; charset=utf-8"))
    with pytest.raises(importer.ImportRefused, match="non un fichier vidéo"):
        importer.import_url(bucket, url, rights_confirmed=True, opener=page)
    denied = FakeOpener(error=urllib.error.HTTPError(url, 403, "Forbidden", None, None))
    with pytest.raises(importer.ImportRefused, match="HTTP 403"):
        importer.import_url(bucket, url, rights_confirmed=True, opener=denied)
    garbage = FakeOpener(FakeResponse(b"\x00" * 4096, "video/mp4"))
    with pytest.raises(importer.ImportRefused, match="Aucun déchiffrement"):
        importer.import_url(bucket, url, rights_confirmed=True, opener=garbage)
    assert bucket.list() == []
    handler = importer._SameListRedirects({"media.exemple.org"})
    with pytest.raises(importer.ImportRefused):
        handler.redirect_request(None, None, 302, "Found", {}, "https://ailleurs.net/episode.mp4")


def test_import_stores_video_and_never_replaces_a_source(bucket, monkeypatch, tiny_video):
    monkeypatch.setenv(config.ENV_IMPORT_HOSTS, " Media.Exemple.org , cdn.exemple.org")
    url = "https://media.exemple.org/saison%201/mon%20episode.mp4?jeton=abc"
    data = tiny_video.read_bytes()
    key = importer.import_url(bucket, url, rights_confirmed=True, opener=FakeOpener(FakeResponse(data, "video/mp4")))
    assert key == "sources/mon_episode.mp4" and bucket.read(key) == data
    record = bucket.read("state/imports/mon_episode.mp4.json").decode()
    assert "media.exemple.org" in record and "jeton" not in record
    with pytest.raises(importer.ImportRefused, match="jamais remplacée"):
        importer.import_url(bucket, url, rights_confirmed=True, opener=FakeOpener(FakeResponse(b"x", "video/mp4")))
    assert bucket.read(key) == data
