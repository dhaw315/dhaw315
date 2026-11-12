import json
import types

import pytest

from conftest import EPISODE_SRT, needs_ffmpeg
from ytclips import cli, config, ledger, proposals, storage, tiktok, youtube

RIGHTS = "--rights-confirmed"


class HttpError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.resp = types.SimpleNamespace(status=status)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv(config.ENV_STORAGE_DIR, str(tmp_path / "bucket"))
    return storage.from_env()


def fake_youtube(monkeypatch, fail=False):
    sent = []

    def upload(creds, clip, body):
        if fail:
            raise HttpError(403)
        sent.append(body["snippet"]["title"])
        return {"id": f"vid{len(sent)}", "status": {"privacyStatus": "private"}}

    monkeypatch.setattr(youtube, "load_credentials", lambda: object())
    monkeypatch.setattr(youtube, "upload_private", upload)
    return sent


def youtube_state(store):
    return sorted((o.key, store.read(o.key)) for o in store.list("state/history/"))


def test_caption_rules():
    assert tiktok.check_caption("  Bonjour #clip  ") == "Bonjour #clip"
    assert tiktok.check_caption("é" * 2200)
    for bad in ("   ", "a" * 2201, "😀" * 1101):  # un émoji compte pour deux unités UTF-16
        with pytest.raises(Exception):
            tiktok.check_caption(bad)


@needs_ffmpeg
def test_tiktok_history_is_independent_from_youtube(store, tiny_video, monkeypatch, capsys):
    store.upload(tiny_video, "sources/a.mp4")
    store.write("sources/a.json", json.dumps({"title": "Titre YouTube"}).encode())
    sent = fake_youtube(monkeypatch)
    assert cli.main(["batch", RIGHTS]) == 0 and sent == ["Titre YouTube"]
    before = youtube_state(store)

    # Un envoi YouTube n'empêche pas TikTok.
    assert cli.main(["tiktok", "prepare", "--dry-run"]) == 0 and store.list("state/tiktok/") == []
    assert cli.main(["tiktok", "prepare"]) == 0
    items = tiktok.records(store)
    assert len(items) == 1
    item = items[0]
    assert (item["status"], item["caption"], item["platform"]) == ("ready", "Titre YouTube", "tiktok")
    assert item["clip_key"].startswith("clips/a_0-24000_") and item["clip_key"].endswith(".mp4")
    ident = item["content_key"][:10]

    # Légende modifiable, jamais écrasée par une nouvelle préparation.
    assert cli.main(["tiktok", "caption", ident, "--text", "Ma légende #extrait"]) == 0
    assert cli.main(["tiktok", "caption", ident, "--text", "  "]) == cli.EXIT_ERROR
    assert cli.main(["tiktok", "prepare"]) == 0
    assert [r["caption"] for r in tiktok.records(store)] == ["Ma légende #extrait"]

    capsys.readouterr()
    assert cli.main(["tiktok", "link", ident]) == 0
    out = capsys.readouterr().out
    assert "Ma légende #extrait" in out and item["clip_key"] in out
    assert cli.main(["tiktok", "link", ident, "--hours", "500"]) == cli.EXIT_ERROR

    # Suivi manuel.
    assert cli.main(["tiktok", "done", ident, "--url", "http://pas-https"]) == cli.EXIT_ERROR
    assert cli.main(["tiktok", "done", ident, "--url", "https://www.tiktok.com/@moi/video/1"]) == 0
    done = tiktok.records(store)[0]
    assert (done["status"], done["post_url"]) == ("posted", "https://www.tiktok.com/@moi/video/1") and done["posted_at"]
    assert cli.main(["tiktok", "skip", ident, "--reason", "doublon"]) == 0
    assert cli.main(["tiktok", "reopen", ident]) == 0
    assert tiktok.records(store)[0]["status"] == "ready"
    assert cli.main(["tiktok", "done", "inconnu"]) == cli.EXIT_ERROR
    capsys.readouterr()
    assert cli.main(["tiktok", "list"]) == 0 and "à publier" in capsys.readouterr().out

    # Rien de ce qui précède n'a touché l'historique YouTube, et YouTube ne renvoie rien.
    assert youtube_state(store) == before
    assert cli.main(["batch", RIGHTS]) == 0 and sent == ["Titre YouTube"]


@needs_ffmpeg
def test_youtube_failure_does_not_block_tiktok(store, tiny_video, monkeypatch):
    store.upload(tiny_video, "sources/a.mp4")
    fake_youtube(monkeypatch, fail=True)
    assert cli.main(["batch", RIGHTS]) == cli.EXIT_ERROR
    assert [r["status"] for r in ledger.Ledger(store).records()] == ["failed"]
    assert cli.main(["tiktok", "prepare"]) == 0  # le clip existe dans le stockage malgré le refus de YouTube
    assert [r["status"] for r in tiktok.records(store)] == ["ready"]
    assert [r["status"] for r in ledger.Ledger(store).records()] == ["failed"]


@needs_ffmpeg
def test_prepare_follows_proposal_decisions(store, episode, monkeypatch):
    store.upload(episode, "sources/episode.mp4")
    store.write("sources/episode.srt", EPISODE_SRT.encode())
    fake_youtube(monkeypatch)
    assert cli.main(["propose"]) == 0 and cli.main(["review", RIGHTS]) == 0
    first, second, _ = (p["content_key"] for p in proposals.Book(store).all())
    assert cli.main(["approve", first[:10], "--title", "Titre retenu"]) == 0
    assert cli.main(["reject", second[:10]]) == 0
    assert cli.main(["tiktok", "prepare"]) == 0
    items = {r["content_key"]: r for r in tiktok.records(store)}
    assert len(items) == 2 and second not in items
    assert items[first]["caption"] == "Titre retenu"


def test_presigned_link_on_s3(monkeypatch):
    moto = pytest.importorskip("moto")
    monkeypatch.setenv(config.ENV_S3_BUCKET, "essai")
    monkeypatch.setenv(config.ENV_S3_ACCESS_KEY, "cle-de-test")
    monkeypatch.setenv(config.ENV_S3_SECRET_KEY, "secret-de-test")
    monkeypatch.setenv(config.ENV_S3_REGION, "us-east-1")
    with moto.mock_aws():
        s3 = storage.from_env()
        s3.client.create_bucket(Bucket="essai")
        url = s3.presign("clips/a.mp4", 3600)
    assert "clips/a.mp4" in url and "X-Amz-Expires=3600" in url and "X-Amz-Signature=" in url
    assert "secret-de-test" not in url
