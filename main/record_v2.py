#!/usr/bin/env python3
"""
VOT Radio Denpasar — Stream Recorder v2.0 (Subprocess Piping & Continuous Streaming Architecture).
"""

import os
import sys
import time
import signal
import argparse
import requests
import subprocess
import threading
from pathlib import Path

# Pastikan folder main ada di sys.path
_MAIN_DIR = Path(__file__).resolve().parent
if str(_MAIN_DIR) not in sys.path:
    sys.path.insert(0, str(_MAIN_DIR))

from core.config import IA_ACCESS_KEY, IA_SECRET_KEY, STREAM_URL, validate_credentials
from core.logger import (
    log, now_wita, print_help_banner, start_stats_writer, init_console, get_headers, CONSOLE
)
import core.proxy as proxy_module
from core.timing import (
    wait_for_stream, make_base_no_ext, parse_start_time, wait_until_start_time
)
from core.uploader import upload_to_archive, write_env_variables

ARGS = None


def run_ffmpeg(url, suffix="", position=0, no_upload=False):
    """
    Rekam stream audio dan upload menggunakan arsitektur Subprocess Piping v2.0.
    Python (requests) ──stdin pipe──► FFmpeg (background, hidup terus).
    Outer Loop menangani reconnect; FFmpeg TIDAK direstart saat koneksi putus.
    Hasil akhir: SATU file utuh tanpa proses merge.
    """
    if ARGS and ARGS.no_record:
        log("[SKIP] --no-record aktif. Perekaman ffmpeg dilewati.")
        return

    date_str = now_wita().strftime("%d-%m-%y")
    os.makedirs("recordings", exist_ok=True)

    if ARGS and ARGS.stream_file_format:
        ext = ARGS.stream_file_format.lower().strip(".")
        log(f"[CODEC] Format file stream (manual): {ext}")
    else:
        ext = "mp3"
        log("[CODEC] Menggunakan format default: mp3 (pipe mode, ffprobe dilewati)")

    if ARGS and ARGS.output_name:
        filename = f"recordings/{ARGS.output_name}.{ext}"
    else:
        base_no_ext = make_base_no_ext(date_str, suffix)
        filename = f"{base_no_ext}.{ext}"

    cmd = [
        "./ffmpeg", "-y", "-hide_banner",
        "-i", "pipe:0",
        "-c", "copy",
        "-metadata", f"title=VOT Denpasar {date_str}",
        "-metadata", "artist=VOT Radio Denpasar",
        "-metadata", f"date={date_str}",
    ]

    if ARGS and ARGS.duration:
        cmd.extend(["-t", str(ARGS.duration)])
        log(f"[RUN] Durasi rekaman dibatasi: {ARGS.duration} detik")

    cmd.append(filename)
    log(f"[RUN] Rekaman dimulai (pipe mode) → {filename}")

    process = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    def _log_ffmpeg_stderr(proc):
        try:
            for raw_line in proc.stderr:
                line = raw_line.decode(errors="replace").strip()
                if line:
                    log(f"[FFMPEG] {line}")
        except Exception:
            pass

    threading.Thread(
        target=_log_ffmpeg_stderr,
        args=(process,),
        daemon=True,
        name="ffmpeg-stderr-reader",
    ).start()

    no_timer = ARGS and ARGS.no_timer_limit
    stop_flag = False
    _duration_start = time.monotonic() if (ARGS and ARGS.duration) else None

    while not stop_flag:
        if CONSOLE and CONSOLE.skip_stage.is_set():
            CONSOLE.skip_stage.clear()
            log("[SKIP] Rekaman dihentikan oleh user (/skip-stage).")
            stop_flag = True
            break

        headers = {"Icy-MetaData": "0"}
        headers.update(get_headers())

        use_proxy_for_stream = False
        if ARGS:
            if ARGS.proxy or getattr(ARGS, "random_proxy", None) in ["stream", "all"]:
                use_proxy_for_stream = True

        proxies = proxy_module.get_proxies_dict(proxy_module.SELECTED_PROXY) if use_proxy_for_stream else None
        if proxies:
            log(f"[PROXY-OK] Stream menggunakan proxy: {proxy_module.SELECTED_PROXY}")

        session = requests.Session()
        session.trust_env = False
        try:
            with session.get(
                url,
                headers=headers,
                proxies=proxies,
                stream=True,
                timeout=10,
            ) as r:
                r.raise_for_status()
                log(f"[OK] Koneksi stream berhasil (HTTP {r.status_code}). Meng-pipe ke FFmpeg...")

                for chunk in r.iter_content(chunk_size=8192):
                    if not chunk:
                        continue

                    try:
                        process.stdin.write(chunk)
                        process.stdin.flush()
                    except (BrokenPipeError, OSError):
                        log("[FAIL] Pipe ke FFmpeg terputus. FFmpeg mungkin crash.")
                        stop_flag = True
                        break

                    now = now_wita()
                    if not no_timer and now.hour == 18 and now.minute >= 30:
                        log("[CUT-OFF] Siaran berakhir pukul 18:30 WITA. Menghentikan perekaman...")
                        stop_flag = True
                        break

                    if _duration_start and (time.monotonic() - _duration_start) >= ARGS.duration:
                        log(f"[DONE] Durasi {ARGS.duration}s tercapai. Menghentikan perekaman...")
                        stop_flag = True
                        break

        except (
            requests.exceptions.RequestException,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            ConnectionError,
            OSError,
        ) as e:
            if use_proxy_for_stream:
                log(f"[WARN] Koneksi stream terputus: {type(e).__name__}. Mencari proxy pengganti...")
                proxy_module.next_proxy_parallel_from_pool()
            else:
                log(f"[WARN] Koneksi stream terputus: {type(e).__name__}. Mencoba menghubungkan kembali dalam 2 detik...")

            time.sleep(2)
            continue
        finally:
            session.close()

    log("[DONE] Menutup pipe ke FFmpeg — menunggu FFmpeg menyelesaikan muxing...")
    try:
        process.stdin.close()
    except Exception:
        pass

    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        log("[WARN] FFmpeg timeout saat shutdown, memaksa berhenti...")
        process.kill()
        process.wait()

    log(f"[DONE] File rekaman selesai → {filename}")

    if ARGS and ARGS.no_save:
        log(f"[SKIP] --no-save aktif. Menghapus file rekaman: {filename}")
        try:
            os.remove(filename)
        except Exception as e:
            log(f"[WARN] Gagal menghapus file: {e}")
        return

    log(f"[UPLOAD-PREP] Siap upload: {filename}")

    if no_upload:
        log(f"[SKIP] Mode tanpa upload aktif. File tersimpan di {filename}")
        return

    if position > 0:
        delay = position * 10
        log(f"[DELAY] Menunggu {delay} detik sebelum upload...")
        time.sleep(delay)
    time.sleep(10)

    archive_url, item_id = upload_to_archive(
        filename,
        access_key=getattr(ARGS, "archive_access", None),
        secret_key=getattr(ARGS, "archive_secret", None)
    )

    if archive_url and item_id:
        log(f"[ARCHIVE] File tersedia di {archive_url}")
        write_env_variables(archive_url, item_id)
    else:
        write_env_variables("None", "None")


def main_recording():
    global ARGS
    validate_credentials()

    parser = argparse.ArgumentParser(
        description="VOT Radio Denpasar — Stream Recorder & Uploader v2.0",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-s", "--suffix", type=str, default="", help="Suffix nama file")
    parser.add_argument("-p", "--position", type=int, default=0, help="Delay upload bertahap")
    parser.add_argument("--skip-check", action="store_true", help="Lewati pengecekan stream")
    parser.add_argument("--no-upload", action="store_true", help="Rekam tanpa upload")
    parser.add_argument("--hide-banner", action="store_true", help="Sembunyikan banner help")
    parser.add_argument("--no-timer-limit", action="store_true", help="Abaikan batasan waktu")
    parser.add_argument("--no-record", action="store_true", help="Skip perekaman ffmpeg")
    parser.add_argument("--no-log", action="store_true", help="Nonaktifkan log output")
    parser.add_argument("--no-save", action="store_true", help="Hapus file setelah selesai")
    parser.add_argument("--archive-access", "--archive-acc", type=str, default=None, dest="archive_access")
    parser.add_argument("--archive-secret", "--archive-sec", type=str, default=None, dest="archive_secret")
    parser.add_argument("--stream-url", type=str, default=None, help="URL stream kustom")
    parser.add_argument("-o", type=str, default=None, dest="output_name", help="Nama output file")
    parser.add_argument("--stream-file-format", type=str, default=None, help="Format file audio")
    parser.add_argument("--duration", type=int, default=None, help="Durasi rekaman dalam detik")
    parser.add_argument("--start-time", type=str, default=None, dest="start_time", help="Waktu mulai WITA")
    parser.add_argument("--proxy", type=str, default=None, help="Manual HTTP proxy")
    parser.add_argument("--random-proxy", type=str, choices=["stats", "stream", "all"])

    ARGS = parser.parse_args()
    init_console(ARGS)

    if not ARGS.hide_banner:
        print_help_banner("v2.0")

    stream_url = ARGS.stream_url if ARGS.stream_url else STREAM_URL
    start_stats_writer()

    if ARGS.random_proxy:
        found = proxy_module.find_working_proxy()
        proxy_module.SELECTED_PROXY = found
    elif ARGS.proxy:
        px = ARGS.proxy.strip()
        for prefix in ("https://", "http://"):
            if px.lower().startswith(prefix):
                px = px[len(prefix):]
                break
        proxy_module.SELECTED_PROXY = px
        log(f"[PROXY-OK] Menggunakan manual proxy: {proxy_module.SELECTED_PROXY}")
    else:
        proxy_module.SELECTED_PROXY = None

    if ARGS.start_time:
        try:
            start_dt = parse_start_time(ARGS.start_time)
            if start_dt > now_wita():
                wait_until_start_time(start_dt)
        except Exception as e:
            log(f"[ERROR] --start-time: {e}")

    if not ARGS.skip_check:
        wait_for_stream(stream_url, ARGS)
    else:
        log("[SKIP] Pengecekan stream dilewati, langsung mulai rekam...")

    run_ffmpeg(stream_url, ARGS.suffix, ARGS.position, no_upload=ARGS.no_upload)
    log("[DONE] Semua tugas selesai.")
    return True


if __name__ == "__main__":
    main_recording()
