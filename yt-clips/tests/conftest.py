import shutil
import subprocess

import pytest

from ytclips import config

S3_ENV = (config.ENV_S3_BUCKET, config.ENV_S3_ACCESS_KEY, config.ENV_S3_SECRET_KEY,
          config.ENV_S3_ENDPOINT, config.ENV_S3_REGION, config.ENV_S3_PREFIX)

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg absent")

EPISODE_SRT = """1
00:00:02,000 --> 00:00:05,500
Tu es rentré tard hier soir.

2
00:00:06,000 --> 00:00:09,500
Oui, je sais, pardon.

3
00:00:10,000 --> 00:00:14,000
Tu aurais pu me prévenir, quand même !

4
00:00:14,500 --> 00:00:18,500
Mon téléphone était éteint,

5
00:00:19,000 --> 00:00:23,000
et je n'avais aucun moyen de te joindre.

6
00:00:23,500 --> 00:00:27,500
D'accord. On en reparle demain.

7
00:00:33,500 --> 00:00:38,000
Alors, tu as trouvé quelque chose ?

8
00:00:38,500 --> 00:00:43,000
Rien du tout. Le dossier a disparu.

9
00:00:43,500 --> 00:00:48,000
Disparu ? Comment ça, disparu ?

10
00:00:48,500 --> 00:00:54,000
Quelqu'un est passé avant nous,

11
00:00:54,500 --> 00:01:00,000
et il a tout emporté.

12
00:01:00,500 --> 00:01:05,500
Il faut prévenir les autres.

13
00:01:12,500 --> 00:01:17,000
Je ne pensais pas te revoir ici.

14
00:01:17,500 --> 00:01:22,000
Moi non plus.

15
00:01:22,500 --> 00:01:28,000
Tu comptes rester longtemps ?

16
00:01:28,500 --> 00:01:33,000
Le temps qu'il faudra.

17
00:01:33,500 --> 00:01:36,500
Alors bienvenue.
"""


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("YTCLIPS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("YTCLIPS_CONFIG_DIR", str(tmp_path / "config"))
    for name in (config.ENV_CLIENT_ID, config.ENV_CLIENT_SECRET, config.ENV_REFRESH_TOKEN, config.ENV_STORAGE_DIR,
                 config.ENV_PUBLIC_MODE, config.ENV_TIMEZONE, config.ENV_IMPORT_HOSTS, *S3_ENV):
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


@pytest.fixture(scope="session")
def episode(tmp_path_factory):
    """100 s, trois plans (coupes à 30 s et 70 s), son présent pendant les trois dialogues seulement."""
    path = tmp_path_factory.mktemp("episode") / "episode.mp4"
    spoken = "between(t,2,28)+between(t,33,66)+between(t,72,97)"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=red:s=320x180:r=5:d=30",
         "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=5:d=40",
         "-f", "lavfi", "-i", "color=c=green:s=320x180:r=5:d=30",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=100",
         "-filter_complex", f"[0:v][1:v][2:v]concat=n=3:v=1:a=0[v];[3:a]volume='if({spoken},1,0)':eval=frame[a]",
         "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", str(path)],
        check=True,
    )
    return path
