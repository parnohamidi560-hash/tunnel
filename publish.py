#!/usr/bin/env python3
"""
fb-publisher v4: ambil video dari GitHub queue lalu upload ke FB Page.

Jalan di VPS milik parno (bukan di Muse) — tanpa approval card.
Dipicu via cron tiap 8 jam.

Pengaman kegagalan:
  - Video HANYA dipindah ke done/ bila Facebook mengembalikan ID sukses.
  - Gagal download/upload karena jaringan: retry otomatis max 3x (antar run),
    tercatat di attempts.json. Masih gagal -> karantina di failed/.
  - Upload yang responsnya HILANG di tengah jalan (timeout/exception):
    TIDAK di-retry otomatis -> karantina, cegah publish dobel.
    Cek manual di page, lalu hapus dari failed/ bila perlu tayang ulang.
  - Token kedaluwarsa/dicabut: run langsung berhenti, tidak spam.
  - published.log: satu video tidak akan dipublish dua kali.
"""
import argparse
import json
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
ATTEMPTS_FILE = BASE / "attempts.json"
LOG_FILE = BASE / "publish.log"
MAX_ATTEMPTS = 3

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
    attempts = load_attempts()
    attempts.pop(filename, None)
    save_attempts(attempts)


def load_attempts() -> dict:
    try:
        return json.loads(ATTEMPTS_FILE.read_text())
    except Exception:
        return {}


def save_attempts(d: dict) -> None:
    ATTEMPTS_FILE.write_text(json.dumps(d))


def note_attempt(filename: str) -> int:
    d = load_attempts()
    d[filename] = d.get(filename, 0) + 1
    save_attempts(d)
    return d[filename]


def quarantine(mp4: Path, reason: str) -> None:
    logging.error("KARANTINA %s: %s", mp4.name, reason)
    try:
        shutil.move(str(mp4), FAILED / mp4.name)
    except Exception as e:
        logging.error("Gagal pindah ke failed/: %s", e)
    d = load_attempts()
    d.pop(mp4.name, None)
    save_attempts(d)


def fetch_manifest() -> list:
    r = requests.get(QUEUE_RAW + "manifest.json", timeout=30)
    r.raise_for_status()
    d = r.json()
    return d.get("videos", [])


def download_video(filename: str, dest: Path) -> bool:
    url = QUEUE_RAW + quote(filename)
    try:
        r = requests.get(url, timeout=300)
        if r.status_code != 200:
            logging.error("Download %s -> HTTP %s", filename, r.status_code)
            return False
        raw = r.content
        if len(raw) < 100000 or b"ftyp" not in raw[:32]:
            logging.error("Download %s: isi bukan mp4 valid (%d bytes)", filename, len(raw))
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
    attempts = load_attempts()

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
        if attempts.get(fn, 0) >= MAX_ATTEMPTS:
            logging.warning("Skip %s: sudah %dx gagal, menunggu tindakan manual", fn, MAX_ATTEMPTS)
            continue

        mp4 = INCOMING / fn
        if not mp4.exists():
            logging.info("Download %s ...", fn)
            if not download_video(fn, mp4):
                c = note_attempt(fn)
                logging.warning("Download %s gagal (percobaan %d/%d)", fn, c, MAX_ATTEMPTS)
                continue

        caption = (v.get("caption") or fn).strip()
        logging.info("Upload %s (%d bytes)", fn, mp4.stat().st_size)
        try:
            res = upload_video(mp4, caption, token)
        except Exception as e:
            # Respons hilang di tengah jalan -> JANGAN retry otomatis (risiko dobel).
            quarantine(mp4, f"upload exception (hasil tidak pasti): {e}. "
                            "Cek manual di page; hapus dari failed/ bila perlu tayang ulang.")
            continue

        body = res["body"]
        if res["status"] == 200 and body.get("id"):
            logging.info("OK %s -> video id %s", fn, body["id"])
            mark_published(fn)
            shutil.move(str(mp4), DONE / mp4.name)
            n += 1
        else:
            err = str(body).lower()
            if "expired" in err or "permission" in err or "oauth" in err:
                logging.error("Token bermasalah — hentikan run ini: %s", str(body)[:200])
                break
            c = note_attempt(fn)
            if c >= MAX_ATTEMPTS:
                quarantine(mp4, f"upload gagal {c}x: {res['status']} {str(body)[:200]}")
            else:
                logging.warning("Upload %s gagal (percobaan %d/%d), retry run berikutnya",
                                fn, c, MAX_ATTEMPTS)
    if n == 0:
        logging.info("Tidak ada video baru untuk dipublish.")


if __name__ == "__main__":
    main()
