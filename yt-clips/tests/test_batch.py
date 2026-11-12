import json
import shutil
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ytclips import batch, cli, config, ledger, media, storage, youtube

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg absent")
BATCH = ["batch", "--rights-confirmed"]
S3_ENV = (config.ENV_S3_BUCKET, config.ENV_S3_ACCESS_KEY, config.ENV_S3_SECRET_KEY,
          config.ENV_S3_ENDPOINT, config.ENV_S3_REGION, config.ENV_S3_PREFIX)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("YTCLIPS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("YTCLIPS_CONFIG_DIR", str(tmp_path / "config"))
    for name in (config.ENV_CLIENT_ID, config.ENV_CLIENT_SECRET, config.ENV_REFRESH_TOKEN,
                 config.ENV_STORAGE_DIR, *S3_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def tiny_video(tmp_path_factory):
    path = tmp_path_factory.mktemp("media") / "tiny.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=5",
         "-f", "lavfi", "-i", "sine=frequency=440", "-t", "24", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
        check=True,
    )
    return path


@pytest.fixture(params=["local", "s3"])
def store(request, tmp_path, monkeypatch):
    """Le même contrat est vérifié sur un dossier et sur un S3 simulé en mémoire (moto)."""
    if request.param == "local":
        monkeypatch.setenv(config.ENV_STORAGE_DIR, str(tmp_path / "bucket"))
        yield storage.from_env()
        return
    moto = pytest.importorskip("moto")
    monkeypatch.setenv(config.ENV_S3_BUCKET, "essai")
    monkeypatch.setenv(config.ENV_S3_ACCESS_KEY, "cle-de-test")
    monkeypatch.setenv(config.ENV_S3_SECRET_KEY, "secret-de-test")
    monkeypatch.setenv(config.ENV_S3_REGION, "us-east-1")
    monkeypatch.setenv(config.ENV_S3_PREFIX, "ytclips")
    with moto.mock_aws():
        s3 = storage.from_env()
        s3.client.create_bucket(Bucket="essai")
        yield s3


@pytest.fixture
def local_store(tmp_path, monkeypatch):
    monkeypatch.setenv(config.ENV_STORAGE_DIR, str(tmp_path / "bucket"))
    return storage.from_env()


@pytest.fixture
def uploads(monkeypatch):
    sent = []

    def upload(creds, clip, body):
        assert body["status"]["privacyStatus"] == "private"
        sent.append(body)
        return {"id": f"vid{len(sent)}", "status": {"privacyStatus": "private"}}

    monkeypatch.setattr(youtube, "load_credentials", lambda: object())
    monkeypatch.setattr(youtube, "upload_private", upload)
    return sent


def add_source(store, video, name, spec=None):
    store.upload(video, f"sources/{name}.mp4")
    if spec is not None:
        store.write(f"sources/{name}.json", json.dumps(spec).encode())


def statuses(store):
    return [(r["attempt"], r["status"], r.get("phase"), r.get("kind")) for r in ledger.Ledger(store).records()]


class HttpError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.resp = types.SimpleNamespace(status=status)


# --- stockage ----------------------------------------------------------------

def test_storage_contract(store, tmp_path):
    assert store.read("a/x.json") is None
    assert store.write("a/x.json", b"un", if_absent=True) is True
    assert store.write("a/x.json", b"deux", if_absent=True) is False
    assert store.read("a/x.json") == b"un"
    assert store.write("a/x.json", b"trois") is True
    assert store.read("a/x.json") == b"trois"

    local = tmp_path / "f.bin"
    local.write_bytes(b"0123456789")
    store.upload(local, "b/f.bin")
    store.download("b/f.bin", tmp_path / "back.bin")
    assert (tmp_path / "back.bin").read_bytes() == b"0123456789"
    assert [(o.key, o.size) for o in store.list("b/")] == [("b/f.bin", 10)]
    assert {o.key for o in store.list()} == {"a/x.json", "b/f.bin"}
    store.delete("b/f.bin")
    assert store.list("b/") == []
    with pytest.raises(storage.StorageError):
        store.download("absent.bin", tmp_path / "nope")


def test_self_test_sees_previous_execution(store):
    first = storage.self_test(store)
    assert first["read_write"] and first["exclusive"] and first["previous"] is None
    second = storage.self_test(store)
    assert second["previous"]["at"] == first["current"]["at"]
    assert [o.key for o in store.list("state/checks/")] == ["state/checks/last.json"]


def test_self_test_detects_service_ignoring_the_condition(tmp_path):
    class Sloppy(storage.LocalStorage):
        def write(self, key, data, if_absent=False):
            return super().write(key, data)

    assert storage.self_test(Sloppy(tmp_path / "s"))["exclusive"] is False


def test_incomplete_s3_configuration_is_reported(monkeypatch):
    assert storage.from_env() is None
    monkeypatch.setenv(config.ENV_S3_BUCKET, "b")
    with pytest.raises(storage.StorageError, match=config.ENV_S3_SECRET_KEY):
        storage.from_env()
    assert cli.main(BATCH) == cli.EXIT_ERROR


# --- historique partagé ------------------------------------------------------

def test_ledger_claim_is_exclusive_and_decisions(store):
    book = ledger.Ledger(store)
    key = ledger.content_key("sha", 0, 20_000)
    assert book.decide(key, 3600, 3).action == ledger.TAKE
    first = book.claim(key, 1, {"source_key": "sources/a.mp4"})
    assert first is not None and book.claim(key, 1, {"source_key": "autre"}) is None
    assert book.decide(key, 3600, 3).action == ledger.BUSY
    assert book.decide(key, 0, 3).action == ledger.TAKE  # réservation périmée, avant tout envoi

    book.update(first, phase=ledger.UPLOADING)
    assert book.decide(key, 0, 3).action == ledger.BLOCKED  # jamais reprise automatiquement
    assert book.release(key[:10])["kind"] == "released"
    assert book.decide(key, 3600, 1).action == ledger.TAKE  # une libération ne consomme pas d'essai

    second = book.claim(key, 2, {"source_key": "sources/a.mp4"})
    book.fail(second, "error", "boum")
    assert book.decide(key, 3600, 1).action == ledger.EXHAUSTED
    third = book.claim(key, 3, {"source_key": "sources/a.mp4"})
    book.update(third, status=ledger.UPLOADED, video_id="v")
    assert book.decide(key, 0, 99).action == ledger.DONE


def test_conflict_detected_before_upload(local_store):
    book = ledger.Ledger(local_store)
    key = ledger.content_key("sha", 0, 20_000)
    slow = book.claim(key, 1, {})
    assert book.conflict(slow) is None
    fast = book.claim(key, 2, {})
    assert "remplacée" in book.conflict(slow)
    book.update(slow, phase=ledger.UPLOADING)
    assert "déjà engagé" in book.conflict(fast)


# --- autorisation Google -----------------------------------------------------

def test_expired_refresh_token_gives_clear_auth_error(monkeypatch):
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials

    calls = []

    def refuse(self, request):
        calls.append(1)
        raise RefreshError("invalid_grant: Token has been expired or revoked.", {"error": "invalid_grant"})

    monkeypatch.setattr(Credentials, "refresh", refuse)
    monkeypatch.setenv(config.ENV_CLIENT_ID, "id")
    monkeypatch.setenv(config.ENV_CLIENT_SECRET, "secret-client")
    monkeypatch.setenv(config.ENV_REFRESH_TOKEN, "jeton-expire")
    with pytest.raises(youtube.AuthError) as caught:
        youtube.load_credentials()
    message = str(caught.value)
    assert "A EXPIRÉ" in message and "ytclips auth" in message and config.ENV_REFRESH_TOKEN in message
    assert "jeton-expire" not in message and "secret-client" not in message
    assert calls == [1]  # un seul essai, pas de boucle


def test_batch_stops_on_expired_auth_without_touching_storage(local_store, tiny_video, monkeypatch, capsys):
    from google.auth.exceptions import RefreshError

    add_source(local_store, tiny_video, "a")
    before = {o.key for o in local_store.list()}

    def expired():
        raise youtube.auth_error(RefreshError("invalid_grant: expired", {"error": "invalid_grant"}))

    monkeypatch.setattr(youtube, "load_credentials", expired)
    monkeypatch.setattr(youtube, "upload_private", lambda *a: pytest.fail("envoi tenté"))
    assert cli.main(BATCH) == cli.EXIT_AUTH
    assert "L'AUTORISATION GOOGLE A EXPIRÉ" in capsys.readouterr().err
    assert {o.key for o in local_store.list()} == before


# --- lot ---------------------------------------------------------------------

@needs_ffmpeg
def test_batch_uploads_each_clip_once(store, tiny_video, uploads):
    add_source(store, tiny_video, "a", {"start": "2", "end": "23", "title": "Lot", "tags": "x, y"})
    assert cli.main(BATCH) == cli.EXIT_OK
    assert [body["snippet"]["title"] for body in uploads] == ["Lot"]
    assert uploads[0]["snippet"]["tags"] == ["x", "y"]
    assert statuses(store) == [(1, "uploaded", "finished", None)]
    record = ledger.Ledger(store).records()[0]
    assert (record["video_id"], record["privacy"], record["start_ms"], record["end_ms"]) == ("vid1", "private", 2000, 23000)
    clips = [o.key for o in store.list("clips/")]
    assert record["clip_key"] in clips and len(clips) == 2
    assert len(store.list("state/runs/")) == 1

    # Nouvelle exécution : rien à faire.
    assert cli.main(BATCH) == cli.EXIT_OK
    # Même vidéo et mêmes bornes sous un autre nom : reconnue par son contenu.
    add_source(store, tiny_video, "copie", {"start": "2", "end": "23"})
    assert cli.main(BATCH) == cli.EXIT_OK
    assert len(uploads) == 1 and len(statuses(store)) == 1


@needs_ffmpeg
def test_dry_run_writes_nothing(local_store, tiny_video, monkeypatch):
    monkeypatch.setattr(youtube, "load_credentials", lambda: pytest.fail("identifiants lus en simulation"))
    monkeypatch.setattr(youtube, "upload_private", lambda *a: pytest.fail("envoi tenté"))
    add_source(local_store, tiny_video, "a")  # sans fiche : de 0 à la fin (24 s)
    before = {o.key for o in local_store.list()}
    outcomes, ok = batch.run(local_store, limit=1, dry_run=True, stale_seconds=3600, max_attempts=3)
    assert ok and [o.result for o in outcomes] == [batch.SIMULATED]
    assert {o.key for o in local_store.list()} == before


@needs_ffmpeg
def test_limit_and_distinct_ranges(local_store, tiny_video, uploads):
    for name, start in (("a", 0), ("b", 1), ("c", 2)):
        add_source(local_store, tiny_video, name, {"start": str(start), "end": str(start + 20)})
    assert cli.main([*BATCH, "--limit", "2"]) == cli.EXIT_OK
    assert len(uploads) == 2
    assert cli.main([*BATCH, "--limit", "2"]) == cli.EXIT_OK
    assert len(uploads) == 3
    assert cli.main([*BATCH, "--limit", "2"]) == cli.EXIT_OK
    assert len(uploads) == 3


@needs_ffmpeg
def test_interrupted_upload_blocks_until_released(local_store, tiny_video, uploads, monkeypatch):
    add_source(local_store, tiny_video, "a")
    working_upload = youtube.upload_private
    monkeypatch.setattr(youtube, "upload_private", lambda *a: (_ for _ in ()).throw(ConnectionError("coupure")))
    assert cli.main(BATCH) == cli.EXIT_ERROR
    assert statuses(local_store) == [(1, "pending", "uploading", None)]

    monkeypatch.setattr(youtube, "upload_private", working_upload)
    assert cli.main([*BATCH, "--stale-hours", "0"]) == cli.EXIT_OK  # bloqué, même réservation « périmée »
    assert uploads == []

    key = ledger.Ledger(local_store).records()[0]["content_key"]
    assert cli.main(["history", "--remote", "--release", key[:12]]) == cli.EXIT_OK
    assert cli.main(BATCH) == cli.EXIT_OK
    assert len(uploads) == 1
    assert [s[1] for s in statuses(local_store)] == ["failed", "uploaded"]


@needs_ffmpeg
def test_refused_upload_is_retried_then_abandoned(local_store, tiny_video, monkeypatch):
    calls = []

    def refuse(*args):
        calls.append(1)
        raise HttpError(403)

    monkeypatch.setattr(youtube, "load_credentials", lambda: object())
    monkeypatch.setattr(youtube, "upload_private", refuse)
    add_source(local_store, tiny_video, "a")
    for _ in range(5):
        cli.main(BATCH)
    assert len(calls) == 3  # trois essais, puis plus aucun
    assert [s[1] for s in statuses(local_store)] == ["failed"] * 3


@needs_ffmpeg
def test_auth_refused_during_upload(local_store, tiny_video, monkeypatch):
    monkeypatch.setattr(youtube, "load_credentials", lambda: object())
    monkeypatch.setattr(youtube, "upload_private", lambda *a: (_ for _ in ()).throw(HttpError(401)))
    add_source(local_store, tiny_video, "a")
    assert cli.main(BATCH) == cli.EXIT_AUTH
    assert statuses(local_store) == [(1, "failed", "uploading", "auth")]


@needs_ffmpeg
def test_stale_claim_before_upload_is_taken_over(local_store, tiny_video, uploads):
    add_source(local_store, tiny_video, "a")
    book = ledger.Ledger(local_store)
    key = ledger.content_key(media.sha256_file(tiny_video), 0, 24_000)
    dead = book.claim(key, 1, {"source_key": "sources/a.mp4", "start_ms": 0, "end_ms": 24_000})

    assert cli.main(BATCH) == cli.EXIT_OK  # réservation récente : on n'y touche pas
    assert uploads == []

    dead["updated_at"] = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat(timespec="seconds")
    local_store.write(book._key(key, 1), json.dumps(dead).encode())
    assert cli.main(BATCH) == cli.EXIT_OK
    assert len(uploads) == 1
    assert [(s[0], s[1]) for s in statuses(local_store)] == [(1, "pending"), (2, "uploaded")]


@needs_ffmpeg
def test_invalid_source_is_recorded_not_looped(local_store, tiny_video, uploads):
    add_source(local_store, tiny_video, "a", {"start": "0", "end": "5"})  # 5 s : trop court
    local_store.write("sources/casse.mp4", b"pas une video")
    for expected in (cli.EXIT_ERROR, cli.EXIT_ERROR, cli.EXIT_ERROR, cli.EXIT_OK, cli.EXIT_OK):
        assert cli.main([*BATCH, "--limit", "5"]) == expected
    assert uploads == []
    assert len(statuses(local_store)) == 6  # 3 essais par source, pas davantage


@needs_ffmpeg
def test_two_simultaneous_processes_upload_once(tmp_path, tiny_video):
    """Deux processus distincts lancés ensemble sur le même stockage."""
    bucket = tmp_path / "bucket"
    add_source(storage.LocalStorage(bucket), tiny_video, "a")
    sent = tmp_path / "sent"
    env = {
        "PATH": __import__("os").environ["PATH"],
        config.ENV_STORAGE_DIR: str(bucket),
        "YTCLIPS_DATA_DIR": str(tmp_path / "data"),
        "YTCLIPS_CONFIG_DIR": str(tmp_path / "config"),
        "FAKE_YT_DIR": str(sent),
        "FAKE_YT_DELAY": "1",
    }
    runner = [sys.executable, str(Path(__file__).with_name("fake_runner.py")), *BATCH]
    processes = [subprocess.Popen(runner, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                 for _ in range(2)]
    outputs = [process.communicate(timeout=180)[0] for process in processes]
    assert [process.returncode for process in processes] == [0, 0], outputs
    assert len(list(sent.glob("*.json"))) == 1, outputs
    assert [s[1] for s in statuses(storage.LocalStorage(bucket))] == ["uploaded"]
