#!/usr/bin/env python3
"""
fb-publisher: upload otomatis video Reels ke Facebook Page via Graph API.

Jalan di VPS milik parno (bukan di Muse) — tanpa approval card.
Dipicu via cron tiap 8 jam. Mengambil .mp4 baru dari ~/fb-publisher/incoming/,
mengupload ke Page, lalu memindahkan ke ~/fb-publisher/done/.

Kebutuhan:
  ~/.fb-publisher/.page_token  (600) — Page Access Token (pages_manage_posts)
  ~/fb-publisher/incoming/*.mp4 + *.txt pendamping (caption)
"""
import os
import sys
import time
import shutil
import logging
from pathlib import Path

import requests

PAGE_ID = "1344318008770197"
GRAPH_VERSION = "v21.0"
BASE = Path.home() / "fb-publisher"
INCOMING = BASE / "incoming"
DONE = BASE / "done"
FAILED = BASE / "failed"
TOKEN_FILE = Path.home() / ".fb-publisher" / ".page_token"
LOG_FILE = BASE / "publish.log"

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def read_token() -> str:
    try:
        tok = TOKEN_FILE.read_text().strip()
    except FileNotFoundError:
        logging.error("Token tidak ditemukan: %s", TOKEN_FILE)
        sys.exit("Token tidak ditemukan — pasang dulu di ~/.fb-publisher/.page_token")
    if not tok:
        sys.exit("Token kosong")
    return tok


def read_caption(mp4_path: Path) -> str:
    txt = mp4_path.with_suffix(".txt")
    if txt.exists():
        try:
            return txt.read_text(encoding="utf-8").strip()[:2000]
        except Exception as e:
            logging.warning("Gagal baca caption %s: %s", txt, e)
    return mp4_path.stem.replace("_", " ")


def upload_video(mp4_path: Path, caption: str, token: str) -> dict:
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PAGE_ID}/videos"
    with open(mp4_path, "rb") as f:
        r = requests.post(
            url,
            data={"description": caption, "access_token": token},
            files={"file": (mp4_path.name, f, "video/mp4")},
            timeout=300,
        )
    try:
        return {"status": r.status_code, "body": r.json()}
    except Exception:
        return {"status": r.status_code, "body": {"raw": r.text[:500]}}


def main() -> None:
    for d in (INCOMING, DONE, FAILED):
        d.mkdir(parents=True, exist_ok=True)
    token = read_token()

    videos = sorted(INCOMING.glob("V3_*.mp4"))
    if not videos:
        logging.info("Tidak ada video baru.")
        return

    for mp4 in videos:
        caption = read_caption(mp4)
        logging.info("Upload %s (%s)", mp4.name, mp4.stat().st_size)
        try:
            res = upload_video(mp4, caption, token)
        except Exception as e:
            logging.error("Upload %s gagal (exception): %s", mp4.name, e)
            continue
        body = res["body"]
        if res["status"] == 200 and body.get("id"):
            logging.info("OK %s -> post id %s", mp4.name, body["id"])
            shutil.move(str(mp4), DONE / mp4.name)
            txt = mp4.with_suffix(".txt")
            if txt.exists():
                shutil.move(str(txt), DONE / txt.name)
        else:
            logging.error("GAGAL %s: %s %s", mp4.name, res["status"], str(body)[:300])
            # token kedaluwarsa / izin dicabut -> berhenti, jangan spam retry
            err = str(body).lower()
            if "expired" in err or "permission" in err or "oauth" in err:
                logging.error("Kemungkinan token bermasalah — hentikan run ini.")
                break
            shutil.move(str(mp4), FAILED / mp4.name)


if __name__ == "__main__":
    main()
