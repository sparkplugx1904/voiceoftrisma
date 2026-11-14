#!/usr/bin/env python3
"""
VoT Retranscribe Engine — Pemindaian & Transkripsi Ulang Terdistribusi untuk Rekaman Terdampak.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_MAIN_DIR = Path(__file__).resolve().parent
if str(_MAIN_DIR) not in sys.path:
    sys.path.insert(0, str(_MAIN_DIR))

try:
    from core.config import IA_ACCESS_KEY, IA_SECRET_KEY
    from core.logger import log, now_wita
except ImportError:
    from main.core.config import IA_ACCESS_KEY, IA_SECRET_KEY
    from main.core.logger import log, now_wita

USER_AGENT = "VoiceOfTrisma-Retranscribe/1.0"
TARGETS_FILE = _MAIN_DIR / "targets_retranscribe.json"

# Timestamp batas perbaikan (14 Nov 2026, 18:13:00 UTC / 15 Nov 2026, 02:13:00 WITA)
# Berkas transcript.srt dengan mtime >= CUTOFF dianggap sudah diperbaiki.
DEFAULT_CUTOFF_TIMESTAMP = 1794680000


def extract_suffix_rank(filename: str) -> Tuple[int, str]:
    """Ekstrak suffix numerik rekaman untuk memilih suffix terkecil (0 sebelum 1)."""
    m = re.search(r'[-_](\d+)\.mp3$', filename, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1)), filename
        except ValueError:
            pass
    return 999, filename


def get_item_status(item_id: str, cutoff_ts: int = DEFAULT_CUTOFF_TIMESTAMP) -> Dict:
    """Periksa status transkrip item di Archive.org."""
    url = f"https://archive.org/metadata/{item_id}/files"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
                files = data.get("result", [])

                # Cari MP3
                root_mp3s = [
                    f for f in files
                    if f.get("name", "").lower().endswith(".mp3")
                    and "/" not in f.get("name", "")
                    and not f.get("name", "").startswith("raw/")
                    and int(f.get("size", 0) or 0) > 100_000
                ]
                all_mp3s = root_mp3s if root_mp3s else [
                    f for f in files
                    if f.get("name", "").lower().endswith(".mp3")
                    and int(f.get("size", 0) or 0) > 100_000
                ]
                all_mp3s.sort(key=lambda f: extract_suffix_rank(f.get("name", "")))
                audio_file = all_mp3s[0].get("name") if all_mp3s else None
                audio_url = f"https://archive.org/download/{item_id}/{audio_file}" if audio_file else None

                srt_file = next((f for f in files if f.get("name") == "transcript.srt"), None)
                mtime = int(srt_file.get("mtime", 0) or 0) if srt_file else 0
                size = int(srt_file.get("size", 0) or 0) if srt_file else 0

                is_completed = (srt_file is not None and mtime >= cutoff_ts and size > 0)

                return {
                    "item_id": item_id,
                    "is_completed": is_completed,
                    "has_audio": bool(audio_file),
                    "audio_file": audio_file,
                    "audio_url": audio_url,
                    "srt_mtime": mtime,
                    "srt_size": size,
                    "error": None
                }
        except Exception as e:
            if attempt < 3:
                time.sleep(attempt * 1.0)
            else:
                return {
                    "item_id": item_id,
                    "is_completed": False,
                    "has_audio": False,
                    "audio_file": None,
                    "audio_url": None,
                    "srt_mtime": 0,
                    "srt_size": 0,
                    "error": str(e)
                }
    return {"item_id": item_id, "is_completed": False, "has_audio": False, "error": "unknown"}


def cmd_scan(args):
    """Memindai target rekaman, menyaring yang belum selesai, dan menyiapkan matriks runner."""
    log("=" * 60)
    log("🔎 MEMULAI PEMINDAIAN TARGET TRANSKRIPSI ULANG")
    log("=" * 60)

    if args.item_id:
        log(f"Mode Single Item: {args.item_id}")
        st = get_item_status(args.item_id, cutoff_ts=args.cutoff)
        if not st.get("has_audio"):
            log(f"[FATAL] Audio MP3 tidak ditemukan untuk item {args.item_id}!")
            sys.exit(1)
        selected = [{"item_id": args.item_id, "audio_url": st["audio_url"]}]
        total_count = 1
        completed_count = 1 if st["is_completed"] else 0
        pending_count = 0 if st["is_completed"] else 1
    else:
        if not TARGETS_FILE.exists():
            log(f"[FATAL] File target {TARGETS_FILE} tidak ditemukan!")
            sys.exit(1)

        with open(TARGETS_FILE, "r", encoding="utf-8") as f:
            targets = json.load(f)

        total_count = len(targets)
        log(f"Memeriksa {total_count} target rekaman secara paralel (workers=15)...")

        statuses = []
        with ThreadPoolExecutor(max_workers=15) as executor:
            futures = {executor.submit(get_item_status, item_id, args.cutoff): item_id for item_id in targets}
            for fut in as_completed(futures):
                statuses.append(fut.result())

        # Urutkan kembali sesuai urutan prioritas di targets_retranscribe.json
        status_map = {s["item_id"]: s for s in statuses}
        ordered_statuses = [status_map[tid] for tid in targets if tid in status_map]

        completed_items = [s for s in ordered_statuses if s["is_completed"]]
        pending_items = [s for s in ordered_statuses if not s["is_completed"] and s["has_audio"]]
        no_audio_items = [s for s in ordered_statuses if not s["is_completed"] and not s["has_audio"]]

        completed_count = len(completed_items)
        pending_count = len(pending_items)

        log(f"[PROGRESS] Total Target: {total_count} | Selesai: {completed_count} | Belum: {pending_count} | Tanpa Audio: {len(no_audio_items)}")

        limit = args.limit or 15
        selected_candidates = pending_items[:limit]
        selected = [{"item_id": s["item_id"], "audio_url": s["audio_url"]} for s in selected_candidates]

    # Ekspor ke GITHUB_OUTPUT untuk matrix runners
    github_output = os.environ.get("GITHUB_OUTPUT")
    if selected:
        matrix_json = json.dumps(selected)
        has_items = "true"
        log(f"[MATRIX] Menyiapkan {len(selected)} runner paralel:")
        for idx, it in enumerate(selected, 1):
            log(f"  {idx}. {it['item_id']} -> {it['audio_url']}")
    else:
        matrix_json = json.dumps([{"action": "skip", "item_id": "none", "audio_url": ""}])
        has_items = "false"
        log("[MATRIX] Seluruh 351 rekaman telah selesai ditranskripsi ulang! Tidak ada item tertunda.")

    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"retranscribe_matrix={matrix_json}\n")
            f.write(f"has_items={has_items}\n")
            f.write(f"total_count={total_count}\n")
            f.write(f"completed_count={completed_count}\n")
            f.write(f"pending_count={pending_count}\n")
            f.write(f"batch_count={len(selected)}\n")
        log("[ENV] Berhasil menulis output ke GITHUB_OUTPUT.")

    # Ekspor ringkasan ke GITHUB_STEP_SUMMARY
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        pct = (completed_count / total_count * 100) if total_count > 0 else 100
        summary_lines = [
            "## 🔄 Status Transkripsi Ulang Rekaman Terdampak",
            "",
            f"- **Total Target Rekaman**: `{total_count}`",
            f"- **Telah Selesai Diperbaiki**: `{completed_count}` ({pct:.1f}%)",
            f"- **Sisa Belum Selesai**: `{pending_count}`",
            f"- **Batch Diproses Saat Ini**: `{len(selected)} runner paralel`",
            "",
            "| No | Item Identifier | Status Batch | URL Audio |",
            "| :--- | :--- | :--- | :--- |"
        ]
        if selected:
            for idx, it in enumerate(selected, 1):
                summary_lines.append(f"| {idx} | [`{it['item_id']}`](https://archive.org/details/{it['item_id']}) | 🚀 Sedang Dikerjakan | [Unduh MP3]({it['audio_url']}) |")
        else:
            summary_lines.append("| - | *Semua rekaman telah lengkap* | ✅ Selesai | - |")

        with open(step_summary, "a", encoding="utf-8") as f:
            f.write("\n".join(summary_lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description="VoT Retranscribe Engine CLI")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    scan_parser = subparsers.add_parser("scan", help="Pindai target dan siapkan matriks GHA")
    scan_parser.add_argument("--limit", type=int, default=15, help="Jumlah instance runner paralel per batch (default: 15)")
    scan_parser.add_argument("--item-id", type=str, default="", help="ID rekaman tertentu")
    scan_parser.add_argument("--cutoff", type=int, default=DEFAULT_CUTOFF_TIMESTAMP, help="Cutoff mtime perbaikan")

    args = parser.parse_args()

    if args.subcommand == "scan":
        cmd_scan(args)


if __name__ == "__main__":
    main()
