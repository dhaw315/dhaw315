"""Lance la CLI avec un faux YouTube, pour les essais entre processus distincts.

Chaque « envoi » écrit un fichier dans FAKE_YT_DIR : on peut compter les vidéos
qui auraient été créées. Aucune requête ne part vers Google.
"""

import json
import os
import sys
import time
import uuid
from pathlib import Path

from ytclips import cli, youtube

OUT = Path(os.environ["FAKE_YT_DIR"])
OUT.mkdir(parents=True, exist_ok=True)


def fake_upload(creds, clip, body):
    assert body["status"]["privacyStatus"] == "private"
    time.sleep(float(os.environ.get("FAKE_YT_DELAY", "0")))
    if os.environ.get("FAKE_YT_CRASH"):
        os._exit(137)  # coupure brutale en plein envoi
    video_id = "fake" + uuid.uuid4().hex[:7]
    (OUT / f"{video_id}.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return {"id": video_id, "status": {"privacyStatus": "private"}}


youtube.load_credentials = lambda: object()
youtube.upload_private = fake_upload
sys.exit(cli.main(sys.argv[1:]))
