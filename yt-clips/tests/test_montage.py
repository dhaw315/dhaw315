import json
import subprocess

import pytest

from conftest import needs_ffmpeg
from ytclips import analysis, batch, cli, config, ledger, media, montage, montages, selection, storage, tiktok, youtube
from ytclips import transcribe as engine
from ytclips.selection import Signals

RIGHTS = "--rights-confirmed"


def film_rows(end=290.0, punctuation=".", pause_every=10):
    """Un « film » de dialogues : une réplique de 3 s toutes les 3,5 s, vocabulaire différent à chaque fois."""
    rows, t, n = [(0.0, 8.0, "♪ Générique ♪")], 10.0, 0
    while t + 3 <= end:
        n += 1
        rows.append((t, t + 3.0, f"Voici mota{n} motb{n} motc{n}{punctuation}"))
        t += 3.5 + (2.0 if n % pause_every == 0 else 0)
    return rows


def to_cues(rows):
    return selection.make_cues([(round(s * 1000), round(e * 1000), text) for s, e, text in rows])


def to_srt(rows):
    return "".join(
        f"{n}\n{media._srt_stamp(round(s * 1000))} --> {media._srt_stamp(round(e * 1000))}\n{text}\n\n"
        for n, (s, e, text) in enumerate(rows, 1)
    )


def spans(plan):
    return [(segment.start_ms, segment.end_ms) for segment in plan.segments]


def check_plan(plan, cues, duration, cutoff=0.75):
    assert plan is not None
    assert montage.MIN_SEGMENTS <= len(plan.segments) <= montage.MAX_SEGMENTS
    assert sum(end - start for start, end in spans(plan)) == 60_000 == plan.total_ms
    previous = 0
    for start, end in spans(plan):
        assert previous <= start < end <= duration * cutoff
        previous = end
        for cue in cues:  # aucune borne à l'intérieur d'une réplique
            assert not cue.start < start < cue.end and not cue.start < end < cue.end
    assert [s.role for s in plan.segments][0] == "Introduction" and plan.segments[-1].role == "Conclusion"
    assert all(s.role == "Progression" for s in plan.segments[1:-1])
    assert all(s.reason and s.text for s in plan.segments)


# --- choix des passages ------------------------------------------------------

def test_plan_is_exactly_sixty_seconds_and_stops_before_the_ending():
    cues = to_cues(film_rows())
    plan = montage.plan(Signals(300_000, scene_cuts=[100_000, 200_000]), cues, "Mon film")
    check_plan(plan, cues, 300_000)
    assert plan.whole_sentences and plan.limit_ms == 225_000
    assert plan.title == "Mon film — en 60 secondes" and "Mon film" in plan.description
    assert all("Générique" not in s.text for s in plan.segments)
    # Un passage par partie du film : le premier dans le premier tiers, le dernier dans la dernière zone permise.
    assert plan.segments[0].start_ms < 75_000 and plan.segments[-1].start_ms > 150_000
    # Limite plus stricte, génériques écartés à la main.
    early = montage.plan(Signals(300_000), cues, "t", cutoff=0.5, skip_start_ms=30_000)
    check_plan(early, cues, 300_000, cutoff=0.5)
    assert early.segments[0].start_ms >= 30_000


def test_segments_start_and_end_on_whole_sentences():
    rows = film_rows()
    for index in range(2, len(rows), 3):  # une réplique sur trois se poursuit sur la suivante
        start, end, text = rows[index]
        rows[index] = (start, end, text[:-1] + ",")
        if index + 1 < len(rows):
            nxt = rows[index + 1]
            rows[index + 1] = (nxt[0], nxt[1], "et " + nxt[2][0].lower() + nxt[2][1:])
    cues = to_cues(rows)
    plan = montage.plan(Signals(300_000), cues, "t")
    check_plan(plan, cues, 300_000)
    assert plan.whole_sentences
    for start, end in spans(plan):
        inside = [cue for cue in cues if cue.start >= start and cue.end <= end]
        assert inside[0].text[0].isupper() and inside[-1].text.endswith(".")


def test_falls_back_on_pauses_when_subtitles_have_no_punctuation():
    cues = to_cues(film_rows(punctuation="", pause_every=3))
    plan = montage.plan(Signals(300_000), cues, "t")
    check_plan(plan, cues, 300_000)
    assert plan.whole_sentences is False and "pauses" in plan.segments[0].reason


def test_margins_never_cross_a_scene_cut():
    cues = to_cues(film_rows())
    cuts = sorted({cue.start - 300 for cue in cues} | {cue.end + 200 for cue in cues})
    plan = montage.plan(Signals(300_000, scene_cuts=cuts), cues, "t")
    check_plan(plan, cues, 300_000)
    for start, end in spans(plan):
        inside = [cue for cue in cues if cue.start >= start and cue.end <= end]
        assert not [cut for cut in cuts if start < cut < inside[0].start]
        assert not [cut for cut in cuts if inside[-1].end < cut < end]


def test_repeated_dialogue_is_not_used_twice():
    rows = [(s, e, "Toujours cette même phrase répétée encore.") for s, e, _ in film_rows()]
    assert montage.plan(Signals(300_000), to_cues(rows), "t") is None
    assert montage.similarity("Le dossier a disparu hier", "Hier, le dossier a disparu !") == 1.0
    assert montage.similarity("Bonjour tout le monde", "Rien à voir ici") == 0.0


def test_no_plan_when_dialogue_is_too_sparse():
    assert montage.plan(Signals(300_000), to_cues(film_rows(end=40)), "t") is None
    assert montage.plan(Signals(300_000), [], "t") is None


def test_manual_plan_is_validated():
    cues = to_cues(film_rows())
    signals = Signals(300_000)
    speech = [cue for cue in cues if not cue.music]
    # Six groupes de trois répliques de 10 s chacun : 60 s exactement, bornes au ras des répliques.
    ranges = [(speech[i].start, speech[i + 2].end) for i in (0, 3, 10, 13, 20, 23)]
    plan = montage.manual(ranges, signals, cues, "t")
    assert spans(plan) == ranges and plan.segments[0].reason == "Choisi à la main."
    for bad, message in [
        (ranges[:3], "4 à 6"),
        (ranges[:5], "exactement 60"),
        ([ranges[1], ranges[0], *ranges[2:]], "ordre"),
        ([(ranges[0][0] + 1_000, ranges[0][1]), *ranges[1:]], "coupe la réplique"),
    ]:
        with pytest.raises(media.MediaError, match=message):
            montage.manual(bad, signals, cues, "t")


def test_subtitles_follow_the_montage_timeline():
    cues = to_cues([(0, 2, "♪ Musique ♪"), (10, 13, "Un."), (13.5, 16, "Deux."), (100, 103, "Trois.")])
    srt = montage.compose_srt(cues, [(9_800, 16_300), (99_500, 103_500)])
    assert media.parse_srt(srt) == [(200, 3_200, "Un."), (3_700, 6_200, "Deux."), (7_000, 10_000, "Trois.")]


def test_words_are_grouped_into_short_cues():
    words = [(0, 400, " Bonjour"), (450, 900, " à"), (950, 1500, " tous."), (1600, 2000, " Ça"), (2100, 2500, " va"),
             (4000, 4500, " bien"), (4600, 5000, " ?")]
    cues = engine.group_words(words)
    assert cues == [(0, 1500, "Bonjour à tous."), (1600, 2500, "Ça va"), (4000, 5000, "bien ?")]
    assert media.parse_srt(engine.to_srt(cues)) == cues


# --- avec FFmpeg et un stockage ----------------------------------------------

@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv(config.ENV_STORAGE_DIR, str(tmp_path / "bucket"))
    return storage.from_env()


@pytest.fixture
def sent(monkeypatch):
    titles = []

    def upload(creds, clip, body):
        assert body["status"]["privacyStatus"] == "private"
        titles.append(body["snippet"]["title"])
        return {"id": f"vid{len(titles)}", "status": {"privacyStatus": "private"}}

    monkeypatch.setattr(youtube, "load_credentials", lambda: object())
    monkeypatch.setattr(youtube, "upload_private", upload)
    return titles


@needs_ffmpeg
def test_render_keeps_whole_image_or_zooms(film, tmp_path):
    ranges = [(10_000, 11_000), (50_000, 51_000), (120_000, 121_500), (250_000, 250_500)]
    srt = montage.compose_srt(to_cues(film_rows()), ranges)
    whole = media.render_montage(film, tmp_path / "a.mp4", ranges, srt_text=srt)
    assert (whole["width"], whole["height"], whole["has_audio"], whole["subtitles"]) == (1080, 1920, True, True)
    assert abs(whole["duration_ms"] - 4_000) <= 300
    zoomed = media.render_montage(film, tmp_path / "b.mp4", ranges, zoom=1.5, background="black", crop="320:136:0:22")
    assert (zoomed["width"], zoomed["height"], zoomed["subtitles"]) == (1080, 1920, False)
    with pytest.raises(media.MediaError):
        media.render_montage(film, tmp_path / "c.mp4", ranges, zoom=3)
    with pytest.raises(media.MediaError):
        media.render_montage(film, tmp_path / "c.mp4", [(0, 1_000), (400_000, 401_000)])


@needs_ffmpeg
def test_black_bars_are_detected(tmp_path):
    boxed = tmp_path / "bandes.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x136:rate=5",
         "-t", "20", "-vf", "pad=320:180:0:22:black", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(boxed)],
        check=True,
    )
    assert analysis.detect_crop(boxed, media.probe(boxed)) == "320:136:0:22"


@needs_ffmpeg
def test_montage_from_plan_to_private_upload_and_tiktok(store, film, tiny_video, sent, monkeypatch, capsys):
    store.upload(film, "sources/film.mp4")
    store.write("sources/film.srt", to_srt(film_rows()).encode())
    store.upload(tiny_video, "sources/court.mp4")
    assert cli.main(["montage", "plan"]) == cli.EXIT_ERROR  # plusieurs vidéos : --source obligatoire
    store.delete("sources/court.mp4")

    assert cli.main(["montage", "plan", "--dry-run"]) == 0
    assert store.list("state/") == []  # la simulation n'écrit rien
    assert cli.main(["montage", "plan", "--title", "Le Film"]) == 0
    (item,) = montages.records(store)
    assert item["status"] == "planned" and item["total_ms"] == 60_000 and item["title"] == "Le Film — en 60 secondes"
    assert sum(s["duration_ms"] for s in item["segments"]) == 60_000 and 4 <= len(item["segments"]) <= 6
    assert all(s["reason"] and s["start"] and s["end"] and s["end_ms"] <= 225_000 for s in item["segments"])
    assert item["subtitles_origin"] == "fichier .srt fourni"
    assert len(store.list("state/signals/")) == 1 and len(store.list("state/reserved/")) == 1

    # Nouvelle demande : l'analyse enregistrée est réutilisée, le film n'est même pas retéléchargé.
    with monkeypatch.context() as patch:
        patch.setattr(analysis, "analyze", lambda *a: pytest.fail("analyse refaite"))
        patch.setattr(storage.LocalStorage, "download", lambda *a: pytest.fail("film retéléchargé"))
        assert cli.main(["montage", "plan", "--title", "Le Film"]) == 0
    assert len(montages.records(store)) == 1

    assert cli.main(["montage", "send"]) == cli.EXIT_ERROR  # --rights-confirmed obligatoire
    assert cli.main(["montage", "send", RIGHTS]) == 0
    assert sent == ["Le Film — en 60 secondes"]
    (item,) = montages.records(store)
    assert (item["status"], item["video_id"], item["privacy"]) == ("sent", "vid1", "private")

    local = store.root / item["clip_key"]
    produced = media.probe(local)
    assert (produced["width"], produced["height"], produced["has_audio"]) == (1080, 1920, True)
    assert abs(produced["duration_ms"] - 60_000) <= 300
    meta = json.loads(store.read(item["clip_key"][:-4] + ".json"))
    assert meta["clip"]["subtitles"] is True and len(meta["clip"]["segments"]) == len(item["segments"])

    (record,) = ledger.Ledger(store).records()
    assert (record["status"], record["content_key"], record["start_ms"]) == ("uploaded", item["content_key"], -1)
    (fiche,) = tiktok.records(store)
    assert (fiche["content_key"], fiche["clip_key"], fiche["caption"], fiche["status"]) == (
        item["content_key"], item["clip_key"], "Le Film — en 60 secondes", "ready")

    # Rien n'est renvoyé : ni par « montage send », ni par le traitement par lot.
    assert cli.main(["montage", "send", item["content_key"][:10], RIGHTS]) == 0
    assert cli.main(["batch", RIGHTS]) == 0
    assert sent == ["Le Film — en 60 secondes"] and len(ledger.Ledger(store).records()) == 1
    capsys.readouterr()
    assert cli.main(["history", "--remote"]) == 0 and cli.main(["montage", "list"]) == 0
    captured = capsys.readouterr()
    assert f"montage de {len(item['segments'])} passages" in captured.out and "https://youtu.be/vid1" in captured.err


@needs_ffmpeg
def test_embedded_subtitles_and_saved_transcripts_are_reused(store, film, tmp_path, monkeypatch):
    srt = tmp_path / "subs.srt"
    srt.write_text(to_srt(film_rows()), encoding="utf-8")
    mkv = tmp_path / "film.mkv"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(film), "-i", str(srt), "-map", "0", "-map", "1",
         "-c", "copy", "-c:s", "srt", "-metadata:s:s:0", "language=fre", str(mkv)],
        check=True,
    )
    assert media.subtitle_streams(mkv) == [{"position": 0, "codec": "subrip", "language": "fre"}]
    store.upload(mkv, "sources/film.mkv")
    assert cli.main(["montage", "plan"]) == 0
    (item,) = montages.records(store)
    assert item["subtitles_origin"] == "piste de sous-titres intégrée (fre)"
    assert [o.key.rsplit(".", 1)[1] for o in store.list("state/transcripts/")] == ["json", "srt"]
    # La copie enregistrée suffit ensuite : ni extraction, ni transcription.
    monkeypatch.setattr(media, "extract_subtitles", lambda *a: pytest.fail("extraction refaite"))
    monkeypatch.setattr(engine, "transcribe", lambda *a: pytest.fail("transcription lancée"))
    assert cli.main(["transcribe"]) == 0
    assert cli.main(["montage", "plan", "--cutoff", "0.6"]) == 0
    plans = montages.records(store)
    assert len(plans) == 2
    second = min(plans, key=lambda plan: plan["nothing_after_ms"])  # celui limité à 60 % du film
    assert "réutilisée depuis le stockage" in second["subtitles_origin"]


@needs_ffmpeg
def test_film_without_subtitles_needs_a_transcription(store, film, monkeypatch, capsys):
    store.upload(film, "sources/film.mp4")
    assert cli.main(["montage", "plan"]) == cli.EXIT_ERROR
    assert "ytclips transcribe" in capsys.readouterr().err and montages.records(store) == []

    calls = []

    def fake(path, work, model, language):
        calls.append((model, language))
        return to_srt(film_rows()), "fr"

    monkeypatch.setattr(engine, "transcribe", fake)
    assert cli.main(["transcribe", "--model", "tiny", "--language", "fr"]) == 0
    assert cli.main(["transcribe", "--model", "tiny"]) == 0  # déjà enregistrée : pas de second calcul
    assert calls == [("tiny", "fr")]
    assert cli.main(["montage", "plan"]) == 0
    assert "transcription locale" in montages.records(store)[0]["subtitles_origin"]


@needs_ffmpeg
def test_batch_does_not_clip_a_long_source_on_its_own(store, tiny_video, sent, monkeypatch):
    monkeypatch.setattr(batch, "LONG_SOURCE_MS", 20_000)
    store.upload(tiny_video, "sources/long.mp4")
    assert cli.main(["batch", RIGHTS, "--dry-run"]) == 0 and store.list("state/") == []
    assert cli.main(["batch", RIGHTS]) == 0
    assert sent == [] and ledger.Ledger(store).records() == [] and len(store.list("state/reserved/")) == 1
    monkeypatch.setattr(storage.LocalStorage, "download", lambda *a: pytest.fail("retéléchargée"))
    assert cli.main(["batch", RIGHTS]) == 0 and sent == []
