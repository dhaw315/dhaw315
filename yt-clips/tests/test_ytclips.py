import json
import shutil
import subprocess

import pytest

from ytclips import cli, clipmeta, config, history, logs, media

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg absent")

SRT = """1
00:00:01,000 --> 00:00:04,000
Avant l'extrait

2
00:00:04,500 --> 00:00:08,000
À cheval sur le début

3
00:00:10,000 --> 00:00:12,500
Au milieu
sur deux lignes

4
00:00:40,000 --> 00:00:42,000
Après l'extrait
"""


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("YTCLIPS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("YTCLIPS_CONFIG_DIR", str(tmp_path / "config"))
    for name in (config.ENV_CLIENT_ID, config.ENV_CLIENT_SECRET, config.ENV_REFRESH_TOKEN):
        monkeypatch.delenv(name, raising=False)


def test_parse_timecode():
    assert media.parse_timecode("90") == 90_000
    assert media.parse_timecode("1:30") == 90_000
    assert media.parse_timecode("00:01:30,250") == 90_250
    with pytest.raises(media.MediaError):
        media.parse_timecode("abc")


@pytest.mark.parametrize("start,end,ok", [(0, 20_000, True), (5_000, 65_000, True), (0, 19_999, False), (0, 60_001, False), (10, 5, False)])
def test_validate_range(start, end, ok):
    if ok:
        assert media.validate_range(start, end) == end - start
    else:
        with pytest.raises(media.MediaError):
            media.validate_range(start, end)


def test_range_must_fit_in_source():
    with pytest.raises(media.MediaError):
        media.validate_range(10_000, 40_000, source_ms=30_000)


def test_shift_srt_keeps_only_overlapping_cues():
    shifted = media.shift_srt(SRT, offset_ms=5_000, duration_ms=25_000)
    assert "Avant" not in shifted and "Après" not in shifted
    assert "1\n00:00:00,000 --> 00:00:03,000\nÀ cheval sur le début" in shifted
    assert "2\n00:00:05,000 --> 00:00:07,500\nAu milieu\nsur deux lignes" in shifted


def test_filter_keeps_whole_frame():
    for background in ("blur", "black", "#102030"):
        graph = media.build_filter(background, None)
        assert "force_original_aspect_ratio=decrease" in graph
        assert "subtitles" not in graph
    assert "subtitles=filename=subs.srt" in media.build_filter("blur", "subs.srt")
    with pytest.raises(media.MediaError):
        media.validate_background("rouge")


def _meta(**youtube):
    meta = clipmeta.new_meta(
        clip_file="a.mp4", clip_sha256="c", source_name="a.mov", source_sha256="s",
        start_ms=0, end_ms=20_000, background="blur", subtitles=False,
        title="Titre", description="Texte", tags=["un"],
    )
    meta["youtube"].update(youtube)
    return meta


def test_body_is_always_private():
    body = clipmeta.request_body(_meta())
    assert body["status"]["privacyStatus"] == "private"
    assert body["snippet"]["title"] == "Titre"


@pytest.mark.parametrize("change", [{"privacy": "public"}, {"privacy": "unlisted"}, {"title": ""}, {"title": "x" * 101}, {"title": "a<b"}])
def test_invalid_meta_is_refused(change):
    with pytest.raises(clipmeta.MetaError):
        clipmeta.validate(_meta(**change))


def test_upload_refuses_non_private_body():
    from ytclips import youtube

    with pytest.raises(youtube.YouTubeError):
        youtube.upload_private(None, None, {"status": {"privacyStatus": "public"}})


def test_history_blocks_duplicates(tmp_path):
    store = history.History(tmp_path / "h.sqlite3")
    key = history.dedupe_key("src", 0, 20_000)
    first = store.claim(key, "clipA", "a.mov", 0, 20_000, "T")
    with pytest.raises(history.DuplicateError):  # envoi en cours
        store.claim(key, "clipB", "a.mov", 0, 20_000, "T")
    store.mark_uploaded(first, "vid123", "private")
    with pytest.raises(history.DuplicateError):  # même extrait, autre rendu
        store.claim(key, "clipB", "a.mov", 0, 20_000, "T")
    with pytest.raises(history.DuplicateError):  # même fichier
        store.claim("autre", "clipA", "a.mov", 0, 20_000, "T")
    # L'historique survit à la réouverture du fichier.
    assert history.History(tmp_path / "h.sqlite3").rows()[0]["video_id"] == "vid123"
    assert store.claim(key, "clipB", "a.mov", 0, 20_000, "T", force=True) > first


def test_failed_or_released_entries_allow_retry(tmp_path):
    store = history.History(tmp_path / "h.sqlite3")
    key = history.dedupe_key("src", 0, 20_000)
    store.mark_failed(store.claim(key, "c", "a.mov", 0, 20_000, "T"), "HTTP 403")
    pending = store.claim(key, "c", "a.mov", 0, 20_000, "T")
    assert store.release(pending) and not store.release(pending)
    assert store.find_blocking(key, "c") is None


def test_redaction():
    logs.register_secret("mon-secret-client")
    text = logs.redact('mon-secret-client ya29.abcDEF-123 "refresh_token": "1//0abcdefghijkl" Bearer xyz.123')
    for leaked in ("mon-secret-client", "ya29.abc", "0abcdefghijkl", "xyz.123"):
        assert leaked not in text


def test_credentials_dir_inside_repo_is_detected(tmp_path):
    (tmp_path / ".git").mkdir()
    assert config.inside_git_repo(tmp_path / "secrets")
    assert config.inside_git_repo(config.PROJECT_ROOT)


def test_clip_requires_rights_and_local_file(tmp_path):
    source = tmp_path / "v.mp4"
    source.write_bytes(b"x")
    assert cli.main(["clip", str(source), "--start", "0", "--end", "25"]) == cli.EXIT_ERROR
    assert cli.main(["clip", "https://example.com/v.mp4", "--start", "0", "--end", "25", "--rights-confirmed"]) == cli.EXIT_ERROR


@needs_ffmpeg
def test_end_to_end_clip_and_dry_run(tmp_path, capsys):
    source = tmp_path / "source_test.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25",
         "-f", "lavfi", "-i", "sine=frequency=440", "-t", "32", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-c:a", "aac", str(source)],
        check=True,
    )
    srt = tmp_path / "subs.srt"
    srt.write_text(SRT, encoding="utf-8")
    out = tmp_path / "out"
    assert cli.main(["clip", str(source), "--start", "5", "--end", "0:27", "--srt", str(srt), "--title", "Essai",
                     "--rights-confirmed", "--output-dir", str(out)]) == cli.EXIT_OK

    clip_file = out / "source_test_5000-27000.mp4"
    info = media.probe(clip_file)
    assert (info["width"], info["height"], info["has_audio"]) == (1080, 1920, True)
    assert abs(info["duration_ms"] - 22_000) <= 500
    meta_file = clip_file.with_suffix(".json")
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    assert meta["youtube"]["title"] == "Essai" and meta["clip"]["subtitles"] is True

    # Titre modifié à la main, puis simulation : rien n'est envoyé ni inscrit.
    meta["youtube"]["title"] = "Titre retouché"
    clipmeta.save(meta_file, meta)
    assert cli.main(["upload", str(meta_file), "--dry-run"]) == cli.EXIT_OK
    assert history.History(config.history_path()).rows() == []

    # Sans identifiants, un envoi réel échoue proprement et ne réserve rien.
    assert cli.main(["upload", str(meta_file)]) == cli.EXIT_ERROR
    assert history.History(config.history_path()).rows() == []

    # Doublon déjà publié : simulation et envoi réel sont refusés.
    clip = meta["clip"]
    store = history.History(config.history_path())
    row = store.claim(history.dedupe_key(clip["source_sha256"], 5000, 27000), clip["sha256"], "s", 5000, 27000, "T")
    store.mark_uploaded(row, "abc", "private")
    assert cli.main(["upload", str(meta_file), "--dry-run"]) == cli.EXIT_DUPLICATE
    assert cli.main(["upload", str(meta_file)]) == cli.EXIT_DUPLICATE

    # Un clip modifié après coup ne correspond plus à sa fiche.
    clip_file.write_bytes(clip_file.read_bytes() + b"0")
    assert cli.main(["upload", str(meta_file), "--dry-run"]) == cli.EXIT_ERROR
