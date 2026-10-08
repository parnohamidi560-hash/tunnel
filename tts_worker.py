#!/usr/bin/env python3
"""
tts_worker.py — berjalan di VPS parno (internet bersih, tanpa proxy MITM).

Tiap 2 menit via cron: ambil job TTS dari GitHub queue
(parnohamidi560-hash/fb-video-queue), sintesis via edge-tts dengan voice
yang diminta job (default id-ID-ArdiNeural — SAMA seperti di VM),
upload mp3 hasilnya kembali ke queue untuk diambil VM.

Tidak ada approval card — jalan di VPS milik parno sendiri.

Setup sekali:
  1. pip3 install edge-tts requests
  2. Buat GitHub token: github.com/settings/tokens -> Generate new token
     (classic) -> centang scope `repo` -> copy tokennya
  3. cat > ~/.github-tts-token   (paste token, Enter, Ctrl+D)
     chmod 600 ~/.github-tts-token
  4. curl -sL https://raw.githubusercontent.com/parnohamidi560-hash/tunnel/main/tts_worker.py \
        -o ~/fb-publisher/tts_worker.py
  5. Tambah cron:  */2 * * * * /usr/bin/python3 /home/manly/fb-publisher/tts_worker.py

VM otomatis mendeteksi worker ini via heartbeat — tidak ada setting di VM.
"""
import asyncio
import base64
import json
import logging
import re
import sys
import time
from pathlib import Path

import requests

REPO = "parnohamidi560-hash/fb-video-queue"
API = f"https://api.github.com/repos/{REPO}/contents"
BASE = Path.home() / "fb-publisher"
TOKEN_FILE = Path.home() / ".github-tts-token"
LOG_FILE = BASE / "tts_worker.log"
JOB_TTL = 3 * 3600        # job lebih tua dari ini dianggap basi -> hapus
HEARTBEAT_EVERY = 1200    # tulis heartbeat maks tiap 20 menit (hemat commit)

logging.basicConfig(filename=str(LOG_FILE), level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")


def token() -> str:
    try:
        t = TOKEN_FILE.read_text().strip()
    except FileNotFoundError:
        sys.exit(f"Token tidak ada: {TOKEN_FILE} (lihat docstring setup)")
    if not t:
        sys.exit("Token kosong")
    return t


_TOK = None


def gh(method: str, path: str, data: dict = None):
    global _TOK
    if _TOK is None:
        _TOK = token()
    r = requests.request(method, f"{API}/{path}", headers={
        "Authorization": f"token {_TOK}",
        "Accept": "application/vnd.github.v3+json",
    }, json=data, timeout=30)
    if r.status_code in (200, 201):
        return r.json()
    if r.status_code == 404:
        return None
    raise RuntimeError(f"GitHub {method} {path}: {r.status_code} {r.text[:150]}")


def put_file(path: str, content: bytes, message: str) -> None:
    data = {"message": message,
            "content": base64.b64encode(content).decode()}
    ex = gh("GET", path)
    if ex and ex.get("sha"):
        data["sha"] = ex["sha"]
    gh("PUT", path, data)


def delete_file(path: str) -> bool:
    ex = gh("GET", path)
    if not ex or not ex.get("sha"):
        return False
    gh("DELETE", path, {"message": f"tts cleanup {path}",
                        "sha": ex["sha"]})
    return True


def heartbeat() -> None:
    try:
        ex = gh("GET", "tts-worker/heartbeat.json")
        if ex and ex.get("content"):
            ts = json.loads(base64.b64decode(ex["content"]).decode()).get("ts", 0)
            if time.time() - ts < HEARTBEAT_EVERY:
                return
        put_file("tts-worker/heartbeat.json",
                 json.dumps({"ts": time.time()}).encode(), "tts heartbeat")
    except Exception as e:
        logging.warning("heartbeat gagal: %s", e)


def job_epoch(job_id: str) -> float:
    m = re.match(r"task_(\d+)_", job_id)
    return float(m.group(1)) if m else 0.0


def synth(text: str, voice: str, rate: str, out: Path) -> bool:
    import edge_tts

    async def run():
        comm = edge_tts.Communicate(text[:4000], voice, rate=rate)
        await comm.save(str(out))

    try:
        asyncio.run(asyncio.wait_for(run(), timeout=120))
        return out.stat().st_size > 10000
    except Exception as e:
        logging.error("edge-tts gagal: %s: %s", type(e).__name__, str(e)[:150])
        return False


def process(name: str) -> None:
    job_id = name[:-5]  # buang .json
    meta = gh("GET", f"tts-jobs/{name}")
    if not meta:
        return
    try:
        job = json.loads(base64.b64decode(meta["content"]).decode())
    except Exception:
        delete_file(f"tts-jobs/{name}")
        return
    # job basi -> buang (VM sudah menyerah / crash)
    if time.time() - job_epoch(job_id) > JOB_TTL:
        logging.info("job %s basi — hapus", job_id)
        delete_file(f"tts-jobs/{name}")
        delete_file(f"tts-done/{job_id}.mp3")
        return
    # sudah dikerjakan worker lain / VM sudah ambil?
    if gh("GET", f"tts-done/{job_id}.mp3"):
        delete_file(f"tts-jobs/{name}")
        return
    # claim: hapus job dulu agar tidak dikerjakan 2x
    if not delete_file(f"tts-jobs/{name}"):
        return
    voice = job.get("voice") or "id-ID-ArdiNeural"
    rate = job.get("rate") or "+0%"
    logging.info("sintesis %s voice=%s", job_id, voice)
    mp3 = Path(f"/tmp/tts_{job_id}.mp3")
    try:
        if synth(job.get("text", ""), voice, rate, mp3):
            put_file(f"tts-done/{job_id}.mp3", mp3.read_bytes(),
                     f"tts done {job_id}")
            logging.info("job %s selesai", job_id)
        else:
            logging.error("job %s sintesis gagal", job_id)
    finally:
        mp3.unlink(missing_ok=True)


def main() -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    heartbeat()
    try:
        items = gh("GET", "tts-jobs")
    except Exception as e:
        logging.error("list jobs gagal: %s", e)
        return
    if not items:
        return
    if isinstance(items, dict):  # path adalah file, bukan direktori
        return
    for it in items:
        name = it.get("name", "")
        if name.endswith(".json"):
            try:
                process(name)
            except Exception as e:
                logging.error("job %s error: %s", name, e)


if __name__ == "__main__":
    main()
