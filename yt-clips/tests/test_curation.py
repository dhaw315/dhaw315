import json
import types

import pytest

from conftest import EPISODE_SRT, needs_ffmpeg
from ytclips import analysis, cli, config, curation, ledger, media, proposals, storage, youtube

RIGHTS = "--rights-confirmed"
MANAGE = {config.SCOPE_MANAGE, config.SCOPES[0]}


class HttpError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.resp = types.SimpleNamespace(status=status)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv(config.ENV_STORAGE_DIR, str(tmp_path / "bucket"))
    return storage.from_env()


@pytest.fixture
def yt(monkeypatch):
    """Faux YouTube : compte les envois et les changements de visibilité."""
    state = types.SimpleNamespace(uploads=[], privacy={}, publics=[], scopes=set(config.SCOPES), update_error=None)

    def upload(creds, clip, body):
        assert body["status"]["privacyStatus"] == "private"
        video_id = f"vid{len(state.uploads) + 1}"
        state.uploads.append(body["snippet"]["title"])
        state.privacy[video_id] = "private"
        return {"id": video_id, "status": {"privacyStatus": "private"}}

    def status(creds, video_id):
        if video_id not in state.privacy:
            return None
        return {"privacyStatus": state.privacy[video_id], "selfDeclaredMadeForKids": False, "embeddable": True}

    def make_public(creds, video_id, current):
        assert current["privacyStatus"] == "private"
        state.publics.append(video_id)
        if state.update_error:
            error, state.update_error = state.update_error, None
            if isinstance(error, ConnectionError):
                state.privacy[video_id] = "public"  # la demande est passée, la réponse s'est perdue
            raise error
        state.privacy[video_id] = "public"
        return "public"

    monkeypatch.setattr(youtube, "load_credentials", lambda: object())
    monkeypatch.setattr(youtube, "upload_private", upload)
    monkeypatch.setattr(youtube, "video_status", status)
    monkeypatch.setattr(youtube, "make_public", make_public)
    monkeypatch.setattr(youtube, "granted_scopes", lambda creds: state.scopes)
    return state


def add_episode(store, episode, with_srt=True):
    store.upload(episode, "sources/episode.mp4")
    if with_srt:
        store.write("sources/episode.srt", EPISODE_SRT.encode())


def book(store):
    return proposals.Book(store).all()


def slots(store):
    return [json.loads(store.read(o.key)) for o in store.list("state/publications/")]


@needs_ffmpeg
def test_real_ffmpeg_measures_cuts_and_silences(episode):
    signals = analysis.analyze(episode, media.probe(episode))
    assert len(signals.scene_cuts) == 2
    assert abs(signals.scene_cuts[0] - 30_000) <= 400 and abs(signals.scene_cuts[1] - 70_000) <= 400
    assert len(signals.silences) == 4
    for (start, end), (want_start, want_end) in zip(signals.silences, [(0, 2000), (28000, 33000), (66000, 72000), (97000, 100000)]):
        assert abs(start - want_start) <= 300 and abs(end - want_end) <= 300


@needs_ffmpeg
def test_propose_records_three_extracts_once(store, episode, yt):
    add_episode(store, episode)
    before = {o.key for o in store.list()}
    assert cli.main(["propose", "--dry-run"]) == 0
    assert {o.key for o in store.list()} == before  # la simulation n'écrit rien

    assert cli.main(["propose"]) == 0
    items = book(store)
    assert len(items) == 3 and all(p["status"] == "proposed" and p["basis"] == "srt" for p in items)
    blocks = [(1_000, 29_500), (32_000, 67_500), (71_000, 98_500)]
    for item, (low, high) in zip(items, blocks):
        assert low <= item["start_ms"] and item["end_ms"] <= high
        assert 20_000 <= item["end_ms"] - item["start_ms"] <= 60_000
        assert item["title"] and item["description"] and item["reason"] and item["start"] and item["end"]
        assert item["content_key"] == ledger.content_key(item["source_sha256"], item["start_ms"], item["end_ms"])
    assert items[1]["title"] == "Alors, tu as trouvé quelque chose ?"
    assert len(store.list("state/analysis/")) == 1

    # Relance, même forcée : les passages déjà proposés ne reviennent pas.
    assert cli.main(["propose"]) == 0 and cli.main(["propose", "--force"]) == 0
    assert len(book(store)) == 3
    assert yt.uploads == [] and {o.key for o in store.list("sources/")} == {"sources/episode.mp4", "sources/episode.srt"}


@needs_ffmpeg
def test_propose_skips_passages_already_sent_by_batch(store, episode, yt):
    add_episode(store, episode)
    assert cli.main(["batch", RIGHTS]) == 0  # sans fiche : envoie 0 → 60 s
    assert len(yt.uploads) == 1
    assert cli.main(["propose"]) == 0
    items = book(store)
    assert len(items) == 1 and items[0]["start_ms"] >= 60_000


@needs_ffmpeg
def test_propose_without_subtitles_falls_back_on_audio(store, episode, yt, capsys):
    add_episode(store, episode, with_srt=False)
    assert cli.main(["propose"]) == 0
    items = book(store)
    assert len(items) == 3 and all(p["basis"] == "audio" for p in items)
    assert "transcription" in capsys.readouterr().err


@needs_ffmpeg
def test_review_then_validation_then_public_queue(store, episode, yt, monkeypatch, capsys):
    add_episode(store, episode)
    assert cli.main(["propose"]) == 0
    first, second, third = (p["content_key"] for p in book(store))

    # Évaluation : trois clips envoyés en privé, une seule fois.
    assert cli.main(["review"]) == cli.EXIT_ERROR  # --rights-confirmed obligatoire
    assert cli.main(["review", RIGHTS, "--dry-run"]) == 0 and yt.uploads == []
    assert cli.main(["review", RIGHTS]) == 0
    assert len(yt.uploads) == 3 and set(yt.privacy.values()) == {"private"}
    assert [p["video_id"] for p in book(store)] == ["vid1", "vid2", "vid3"]
    assert len(store.list("clips/")) == 6
    assert cli.main(["review", RIGHTS]) == 0 and cli.main(["batch", RIGHTS]) == 0  # batch laisse la main
    assert len(yt.uploads) == 3
    assert [r["status"] for r in ledger.Ledger(store).records()] == ["uploaded"] * 3

    # Décisions.
    assert cli.main(["approve", first[:10], "--title", "Titre retenu"]) == 0
    assert cli.main(["reject", second[:10], "--reason", "trop long"]) == 0
    assert cli.main(["approve", "inconnu"]) == cli.EXIT_ERROR
    assert cli.main(["approve", third[:10], "--title", "a<b"]) == cli.EXIT_ERROR
    states = {p["content_key"]: p for p in book(store)}
    assert (states[first]["status"], states[first]["title"]) == ("approved", "Titre retenu")
    assert (states[second]["status"], states[third]["status"]) == ("rejected", "proposed")
    capsys.readouterr()
    assert cli.main(["proposals"]) == 0
    listing = capsys.readouterr().out
    assert "validé" in listing and "refusé" in listing and "https://youtu.be/vid1 (private)" in listing

    # Privé par défaut : il faut l'option ET la variable.
    assert cli.main(["publish", RIGHTS]) == 0
    assert cli.main(["publish", RIGHTS, "--public"]) == 0
    monkeypatch.setenv(config.ENV_PUBLIC_MODE, "enabled")
    assert cli.main(["publish", RIGHTS]) == 0
    assert cli.main(["publish", RIGHTS, "--public", "--dry-run"]) == 0
    assert yt.publics == [] and slots(store) == []

    # Permission Google manquante : arrêt net, rien de réservé.
    capsys.readouterr()
    assert cli.main(["publish", RIGHTS, "--public"]) == cli.EXIT_AUTH
    assert "ytclips auth --public" in capsys.readouterr().err
    assert yt.publics == [] and slots(store) == []

    # Publication : changement de visibilité, pas de nouvel envoi.
    yt.scopes = MANAGE
    assert cli.main(["publish", RIGHTS, "--public"]) == 0
    assert yt.publics == ["vid1"] and yt.privacy == {"vid1": "public", "vid2": "private", "vid3": "private"}
    assert len(yt.uploads) == 3
    states = {p["content_key"]: p for p in book(store)}
    assert (states[first]["status"], states[first]["privacy"]) == ("published", "public")
    assert [s["phase"] for s in slots(store)] == ["done"]

    # Un clip public par jour.
    assert cli.main(["approve", third[:10]]) == 0
    assert cli.main(["publish", RIGHTS, "--public"]) == 0
    assert yt.publics == ["vid1"]
    monkeypatch.setattr(curation, "_today", lambda: "2099-01-02")
    assert cli.main(["publish", RIGHTS, "--public"]) == 0
    assert yt.publics == ["vid1", "vid3"] and len(slots(store)) == 2

    # Le clip refusé n'est jamais publié ; une proposition publiée ne se re-décide plus.
    monkeypatch.setattr(curation, "_today", lambda: "2099-01-03")
    assert cli.main(["publish", RIGHTS, "--public"]) == 0
    assert yt.privacy["vid2"] == "private" and len(slots(store)) == 2
    assert cli.main(["reject", first[:10]]) == cli.EXIT_ERROR


@needs_ffmpeg
def test_publish_uploads_privately_first_when_not_reviewed(store, episode, yt, monkeypatch):
    add_episode(store, episode)
    cli.main(["propose"])
    item = book(store)[0]
    assert cli.main(["approve", item["content_key"][:10]]) == 0
    assert cli.main(["publish", RIGHTS]) == 0  # mode privé : envoyé, pas publié
    assert len(yt.uploads) == 1 and yt.privacy == {"vid1": "private"} and yt.publics == []
    monkeypatch.setenv(config.ENV_PUBLIC_MODE, "enabled")
    yt.scopes = MANAGE
    assert cli.main(["publish", RIGHTS, "--public"]) == 0
    assert len(yt.uploads) == 1 and yt.privacy == {"vid1": "public"}


@needs_ffmpeg
def test_publish_resumes_after_interruption_without_duplicate(store, episode, yt, monkeypatch):
    add_episode(store, episode)
    cli.main(["propose"])
    cli.main(["review", RIGHTS])
    first, second, _ = (p["content_key"] for p in book(store))
    cli.main(["approve", first[:10]])
    monkeypatch.setenv(config.ENV_PUBLIC_MODE, "enabled")
    yt.scopes = MANAGE

    yt.update_error = ConnectionError("coupure")
    assert cli.main(["publish", RIGHTS, "--public"]) == cli.EXIT_ERROR
    assert [s["phase"] for s in slots(store)] == ["claimed"]  # le créneau du jour reste réservé
    assert cli.main(["publish", RIGHTS, "--public"]) == 0  # constate que la vidéo est publique
    assert yt.publics == ["vid1"] and book(store)[0]["status"] == "published"
    assert [s["phase"] for s in slots(store)] == ["done"]

    # Refus de YouTube : le créneau est rendu, puis abandon après 3 échecs.
    cli.main(["approve", second[:10]])
    for day in ("2099-02-01", "2099-02-02", "2099-02-03", "2099-02-04", "2099-02-05"):
        monkeypatch.setattr(curation, "_today", lambda day=day: day)
        yt.update_error = HttpError(403)
        cli.main(["publish", RIGHTS, "--public"])
    assert yt.publics.count("vid2") == 3 and yt.privacy["vid2"] == "private"
    assert len(slots(store)) == 1
    assert len(yt.uploads) == 3
