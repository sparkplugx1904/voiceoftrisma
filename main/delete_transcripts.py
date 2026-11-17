#!/usr/bin/env python3
"""
Voice of Trisma — Pembersihan Berkas Transkrip dari Archive.org.

Menghapus seluruh berkas transkrip (transcript.json, transcript.srt, transcript.txt)
pada item Archive.org yang terdaftar di main/targets_retranscribe.json.
Berkas audio (.mp3), deskripsi (description.json), statistik (stats.json),
dan metadata lainnya dijamin AMAN dan TIDAK AKAN disentuh.
"""

import os
import sys
import time
import json
import argparse
import requests
from pathlib import Path
from typing import List, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

_MAIN_DIR = Path(__file__).resolve().parent
if str(_MAIN_DIR) not in sys.path:
    sys.path.insert(0, str(_MAIN_DIR))

# Muat variabel .ENV root jika berjalan lokal
ROOT_DIR = Path(__file__).resolve().parent.parent
try:
    from dotenv import load_dotenv
    for _env_path in [ROOT_DIR / ".ENV", ROOT_DIR / ".env", Path(".ENV"), Path(".env")]:
        if _env_path.is_file():
            load_dotenv(dotenv_path=_env_path)
            break
except ImportError:
    pass

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# WHITELIST KETAT: Hanya berkas transkrip ini yang boleh dihapus
TRANSCRIPT_FILES_WHITELIST = ["transcript.json", "transcript.srt", "transcript.txt"]


def log(msg: str):
    """Mencatat pesan log dengan penanda waktu."""
    now = time.strftime("%H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def delete_transcript_file(
    identifier: str,
    filename: str,
    access_key: str,
    secret_key: str,
    retries: int = 4
) -> Tuple[bool, int, str]:
    """
    Mengirim request HTTP DELETE ke S3 API Archive.org untuk 1 berkas.
    Archive.org mengembalikan HTTP 204 (atau 404 jika file memang belum ada).
    """
    if filename not in TRANSCRIPT_FILES_WHITELIST:
        raise ValueError(f"DILARANG menghapus berkas di luar whitelist: {filename}")

    url = f"https://s3.us.archive.org/{identifier}/{filename}"
    headers = {
        "Authorization": f"LOW {access_key}:{secret_key}",
        "x-archive-cascade-delete": "0",
        "User-Agent": "VoiceOfTrisma-Cleanup/1.0",
    }

    for attempt in range(1, retries + 1):
        try:
            resp = requests.delete(url, headers=headers, timeout=30)
            # HTTP 204 (No Content), 200 (OK), atau 404 (Not Found) dianggap sukses selesai
            if resp.status_code in (200, 204, 404):
                return True, resp.status_code, ""
            elif resp.status_code in (503, 429, 500, 502):
                time.sleep(attempt * 2)
            else:
                return False, resp.status_code, f"HTTP {resp.status_code}: {resp.text[:100]}"
        except Exception as e:
            if attempt < retries:
                time.sleep(attempt * 2)
            else:
                return False, 0, str(e)

    return False, 0, "Timeout/gagal setelah retry."


def process_identifier(
    identifier: str,
    access_key: str,
    secret_key: str
) -> Dict:
    """
    Menghapus seluruh file transkrip (json, srt, txt) untuk 1 item identifier.
    """
    results = {}
    success_count = 0
    errors = []

    for fname in TRANSCRIPT_FILES_WHITELIST:
        ok, code, err_msg = delete_transcript_file(identifier, fname, access_key, secret_key)
        results[fname] = code
        if ok:
            success_count += 1
        else:
            errors.append(f"{fname}: {err_msg}")

    return {
        "identifier": identifier,
        "results": results,
        "success_count": success_count,
        "errors": errors,
    }


def write_summary(
    summary_path: Path,
    total_targets: int,
    completed_items: int,
    total_files_deleted: int,
    failed_items: int,
    elapsed: float,
):
    """Menulis ringkasan Markdown untuk GitHub Actions Step Summary."""
    lines = [
        "## 🗑️ Voice of Trisma — Pembersihan Berkas Transkrip Archive.org",
        "",
        f"- **Total Target Identifier**: `{total_targets}`",
        f"- **Identifier Berhasil Diproses**: `{completed_items}`",
        f"- **Estimasi Berkas Transkrip Dihapus**: `{total_files_deleted}`",
        f"- **Identifier dengan Error**: `{failed_items}`",
        f"- **Durasi Eksekusi**: `{elapsed:.1f} detik`",
        "",
        "> [!NOTE]",
        "> Berkas audio (.mp3), deskripsi (description.json), dan statistik (stats.json) tetap aman dan tidak disentuh.",
    ]
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    log(f"Ringkasan eksekusi ditulis ke {summary_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Pembersihan berkas transkrip Archive.org dari daftar targets_retranscribe.json."
    )
    parser.add_argument(
        "--targets",
        default=str(Path(__file__).resolve().parent / "targets_retranscribe.json"),
        help="Path ke file JSON target identifier.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Jumlah thread pekerja paralel. Default: 8",
    )
    parser.add_argument(
        "--access-key",
        default=os.environ.get("IA_ACCESS_KEY"),
        help="Internet Archive Access Key.",
    )
    parser.add_argument(
        "--secret-key",
        default=os.environ.get("IA_SECRET_KEY"),
        help="Internet Archive Secret Key.",
    )
    parser.add_argument(
        "--export-summary",
        default=os.environ.get("GITHUB_STEP_SUMMARY"),
        help="Path file untuk output GitHub Step Summary.",
    )

    args = parser.parse_args()

    access_key = args.access_key or os.environ.get("IA_ACCESS_KEY")
    secret_key = args.secret_key or os.environ.get("IA_SECRET_KEY")

    if not access_key or not secret_key:
        log("[FATAL] IA_ACCESS_KEY atau IA_SECRET_KEY tidak tersedia!")
        sys.exit(1)

    targets_file = Path(args.targets)
    if not targets_file.exists():
        log(f"[FATAL] File target {targets_file} tidak ditemukan!")
        sys.exit(1)

    try:
        targets: List[str] = json.loads(targets_file.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"[FATAL] Gagal membaca {targets_file}: {e}")
        sys.exit(1)

    total_targets = len(targets)
    log(f"============================================================")
    log(f"🗑️ Voice of Trisma — Pembersihan Berkas Transkrip Archive.org")
    log(f"   Target: {total_targets} identifier dari {targets_file.name}")
    log(f"   Berkas yang dihapus: {', '.join(TRANSCRIPT_FILES_WHITELIST)}")
    log(f"   (Audio .mp3, description.json, stats.json dll. AMAN)")
    log(f"   Workers: {args.workers}")
    log(f"============================================================")

    t0 = time.time()
    completed_items = 0
    total_files_deleted = 0
    failed_items = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_to_id = {
            executor.submit(process_identifier, item_id, access_key, secret_key): item_id
            for item_id in targets
        }

        for future in as_completed(future_to_id):
            completed_items += 1
            item_id = future_to_id[future]
            try:
                res = future.result()
                s_count = res["success_count"]
                total_files_deleted += s_count
                if res["errors"]:
                    failed_items += 1
                    log(f"[{completed_items}/{total_targets}] ⚠ {item_id}: {', '.join(res['errors'])}")
                else:
                    if completed_items % 25 == 0 or completed_items == total_targets:
                        pct = (completed_items / total_targets) * 100
                        log(f"[{completed_items}/{total_targets}] ({pct:.1f}%) ✓ Berkas transkrip berhasil dihapus.")
            except Exception as e:
                failed_items += 1
                log(f"[{completed_items}/{total_targets}] [ERROR] {item_id}: {e}")

    elapsed = time.time() - t0
    log(f"============================================================")
    log(f"🎉 Pembersihan Berkas Transkrip Selesai dalam {elapsed:.1f} detik!")
    log(f"   - Total Identifier Diproses : {completed_items}/{total_targets}")
    log(f"   - Total Berkas Transkrip    : ~{total_files_deleted} berkas")
    log(f"   - Identifier dengan Error   : {failed_items}")
    log(f"============================================================")

    if args.export_summary:
        write_summary(
            Path(args.export_summary),
            total_targets,
            completed_items,
            total_files_deleted,
            failed_items,
            elapsed,
        )


if __name__ == "__main__":
    main()
