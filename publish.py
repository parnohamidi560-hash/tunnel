#!/usr/bin/env python3
"""
fb-publisher v3: ambil video dari GitHub queue lalu upload ke FB Page.

Jalan di VPS milik parno (bukan di Muse) — tanpa approval card.
Dipicu via cron tiap 8 jam.

Alur:
  1. GET manifest dari GitHub (repo publik fb-video-queue)
  2. Download .mp4.b64 yang belum pernah dipublish -> decode -> mp4
  3. Upload ke Facebook Page via Graph API (caption dari manifest)
  4. Tandai published di published.log, pindahkan ke done/

Kebutuhan:
  ~/.fb-publisher/.page_token  (600) — Page Access Token (pages_manage_posts)
"""
import argparse
import base64
import sys
import shutil
import logging
from pathlib import Path
from urllib.parse import quote

import requests

PAGE_ID = "1344318008770197"
GRAPH_VERSION = "v21.0"
QUEUE_RAW = "https://raw.githubusercontent.com/parnohamidi560-hash/fb-video-queue/main/queue/"
BASE = Path.home() / "fb-publisher"
INCOMING = BASE / "incoming"
DONE = BASE / "done"
FAILED = BASE / "failed"
TOKEN_FILE = Path.home() / ".fb-publisher" / ".page_token"
PUBLISHED_LOG = BASE / "published.log"
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


def published_set() -> set:
    if PUBLISHED_LOG.exists():
        return set(x.strip() for x in PUBLISHED_LOG.read_text().splitlines() if x.strip())
    return set()


def mark_published(filename: str) -> None:
    with open(PUBLISHED_LOG, "a") as f:
        f.write(filename + "\n")


def fetch_manifest() -> list:
    r = requests.get(QUEUE_RAW + "manifest.json", timeout=30)
    r.raise_for_status()
    d = r.json()
    return d.get("videos", [])


def download_video(filename: str, dest: Path) -> bool:
    """Download .mp4.b64 dari GitHub lalu decode ke mp4."""
    url = QUEUE_RAW + quote(filename + ".b64")
    try:
        r = requests.get(url, timeout=300)
        if r.status_code != 200:
            logging.error("Download %s -> HTTP %s", filename, r.status_code)
            return False
        raw = base64.b64decode(r.content)
        if len(raw) < 1000 or not raw.startswith(b"\x00\x00\x00"):
            # sanity check longgar: file mp4/ftyp
            if b"ftyp" not in raw[:32]:
                logging.error("Download %s: isi bukan mp4 valid", filename)
                return False
        dest.write_bytes(raw)
        return True
    except Exception as e:
        logging.error("Download %s gagal: %s", filename, e)
        return False


def upload_video(mp4_path: Path, caption: str, token: str) -> dict:
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{PAGE_ID}/videos"
    with open(mp4_path, "rb") as f:
        r = requests.post(
            url,
            data={"description": caption[:2000], "access_token": token},
            files={"file": (mp4_path.name, f, "video/mp4")},
            timeout=300,
        )
    try:
        return {"status": r.status_code, "body": r.json()}
    except Exception:
        return {"status": r.status_code, "body": {"raw": r.text[:500]}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10,
                    help="Maksimal video baru per run (default 10)")
    args = ap.parse_args()

    for d in (INCOMING, DONE, FAILED):
        d.mkdir(parents=True, exist_ok=True)
    token = read_token()
    done = published_set()

    try:
        videos = fetch_manifest()
    except Exception as e:
        logging.error("Gagal ambil manifest GitHub: %s", e)
        return
    logging.info("Manifest: %d video di antrian", len(videos))

    n = 0
    for v in videos:
        fn = v.get("filename", "")
        if not fn or fn in done:
            continue
        if n >= args.limit:
            break
        mp4 = INCOMING / fn
        logging.info("Download %s ...", fn)
        if not download_video(fn, mp4):
            continue
        caption = (v.get("caption") or fn).strip()
        logging.info("Upload %s (%d bytes)", fn, mp4.stat().st_size)
        try:
            res = upload_video(mp4, caption, token)
        except Exception as e:
            logging.error("Upload %s gagal (exception): %s", fn, e)
            continue
        body = res["body"]
        if res["status"] == 200 and body.get("id"):
            logging.info("OK %s -> video id %s", fn, body["id"])
            mark_published(fn)
            shutil.move(str(mp4), DONE / mp4.name)
            n += 1
        else:
            logging.error("GAGAL %s: %s %s", fn, res["status"], str(body)[:300])
            err = str(body).lower()
            if "expired" in err or "permission" in err or "oauth" in err:
                logging.error("Kemungkinan token bermasalah — hentikan run ini.")
                break
            shutil.move(str(mp4), FAILED / mp4.name)
    if n == 0:
        logging.info("Tidak ada video baru untuk dipublish.")


if __name__ == "__main__":
    main()
