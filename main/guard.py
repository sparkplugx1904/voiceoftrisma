#!/usr/bin/env python3
"""
Pipeline Guard for Voice of Trisma (VOT)
========================================
Memeriksa status semua rekaman audio di Archive.org (vot-denpasar-*):
1. Mendeteksi apakah rekaman memiliki:
   - File audio (.mp3)
   - Transkrip (transcript.srt atau transcript.txt)
   - Deskripsi (description.json)
2. Mengkategorikan status:
   - COMPLETE: Sudah ada transkrip dan description.json
   - NEEDS_DESCRIPTION: Sudah ada transkrip, tapi belum ada description.json
   - NEEDS_TRANSCRIPT: Belum ada transkrip maupun description.json
   - IGNORE_NO_AUDIO: Tidak ditemukan file audio mp3
3. Menjalankan aksi:
   - Status & Scan: Laporan kesehatan arsip dan cetak matriks untuk GitHub Actions Dynamic Matrix Job
   - Perbaikan deskripsi langsung (download SRT -> descriptor.py -> ia.py)
   - Fail-safe & resilience terhadap empty matrix dan transient network error
"""

import os
import sys
import re
import json
import time
import shutil
import argparse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Optional, Tuple
from pathlib import Path
try:
    from dotenv import load_dotenv
    _ROOT_DIR = Path(__file__).resolve().parent.parent
    for _candidate in [_ROOT_DIR / ".ENV", _ROOT_DIR / ".env", Path(".ENV"), Path(".env")]:
        if _candidate.is_file():
            load_dotenv(dotenv_path=_candidate)
            break
except ImportError:
    pass

ARCHIVE_SEARCH_URL = (
    "https://archive.org/advancedsearch.php?"
    "q=identifier:vot-denpasar-*&fl[]=identifier,title,publicdate"
    "&sort[]=publicdate+desc&rows={rows}&output=json"
)
ARCHIVE_METADATA_FILES_URL = "https://archive.org/metadata/{identifier}/files"
USER_AGENT = "VoiceOfTrisma-PipelineGuard/1.1"


def log(msg: str):
    print(f"[GUARD] {msg}", flush=True)


def extract_suffix_rank(filename: str) -> Tuple[int, str]:
    """
    Ekstrak suffix numerik rekaman untuk memilih suffix terkecil (0 sebelum 1).
    Format rekaman:
      - VOT-Denpasar_DD-MM-YY-0.mp3  -> (0, ...)
      - VOT-Denpasar_DD-MM-YY-1.mp3  -> (1, ...)
      - VOT-Denpasar_DD-MM-YY.mp3    -> (0, ...) [master tanpa suffix]
    """
    # 1. Format standar VOT dengan tanggal: VOT-Denpasar_DD-MM-YY(YY)[-_]S.mp3
    m_vot = re.search(r'VOT-Denpasar_\d{2}-\d{2}-\d{2,4}[-_](\d+)\.mp3$', filename, re.IGNORECASE)
    if m_vot:
        return (int(m_vot.group(1)), filename)

    # 2. File VOT tanpa suffix numerik setelah tanggal (master tunggal)
    m_base = re.search(r'VOT-Denpasar_\d{2}-\d{2}-\d{2,4}\.mp3$', filename, re.IGNORECASE)
    if m_base:
        return (0, filename)

    # 3. Format generik dengan akhiran [-_](\d+).mp3
    m_gen = re.search(r'[-_](\d+)\.mp3$', filename, re.IGNORECASE)
    if m_gen:
        return (int(m_gen.group(1)), filename)

    return (999, filename)


def is_valid_file(f: Dict, filename: str) -> bool:
    """Periksa apakah file ada dan tidak kosong (ukuran > 0 byte)."""
    if f.get("name") != filename:
        return False
    try:
        size = int(f.get("size", 1))
        return size > 0
    except (ValueError, TypeError):
        return True


def fetch_archive_items(rows: int = 100, retries: int = 3) -> List[Dict]:
    """Ambil daftar item rekaman vot-denpasar-* dari Archive.org dengan retry backoff."""
    url = ARCHIVE_SEARCH_URL.format(rows=rows)
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
                return data.get("response", {}).get("docs", [])
        except Exception as e:
            if attempt < retries:
                time.sleep(attempt * 2)
            else:
                log(f"Gagal mengambil daftar item dari Archive.org ({attempt}/{retries}): {e}")
                return []
    return []


def check_item_status(identifier: str, retries: int = 3) -> Dict:
    """Periksa metadata file di Archive.org untuk 1 item identifier dengan retry backoff."""
    url = ARCHIVE_METADATA_FILES_URL.format(identifier=identifier)
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
                files = data.get("result", [])

                # Cari file MP3: prioritaskan file root (bukan subfolder raw/) yang valid
                root_mp3s = [
                    f for f in files
                    if f.get("name", "").lower().endswith(".mp3")
                    and "/" not in f.get("name", "")
                    and not f.get("name", "").startswith("raw/")
                    and is_valid_file(f, f.get("name", ""))
                ]
                mp3_files = root_mp3s if root_mp3s else [
                    f for f in files
                    if f.get("name", "").lower().endswith(".mp3")
                    and is_valid_file(f, f.get("name", ""))
                ]

                # Sort berdasarkan suffix terkecil (_0 sebelum _1)
                mp3_files.sort(key=lambda f: extract_suffix_rank(f.get("name", "")))

                audio_file = mp3_files[0].get("name") if mp3_files else None
                audio_url = f"https://archive.org/download/{identifier}/{audio_file}" if audio_file else None

                # Cek keberadaan transcript dan description yang valid (>0 byte)
                has_srt = any(is_valid_file(f, "transcript.srt") for f in files)
                has_txt = any(is_valid_file(f, "transcript.txt") for f in files)
                has_transcript = has_srt or has_txt
                has_description = any(is_valid_file(f, "description.json") for f in files)

                srt_name = "transcript.srt" if has_srt else ("transcript.txt" if has_txt else None)
                srt_url = f"https://archive.org/download/{identifier}/{srt_name}" if srt_name else None

                if not mp3_files:
                    action = "IGNORE_NO_AUDIO"
                elif not has_transcript:
                    action = "transcribe_and_describe"
                elif not has_description:
                    action = "describe_only"
                else:
                    action = "COMPLETE"

                return {
                    "item_id": identifier,
                    "action": action,
                    "audio_url": audio_url,
                    "srt_url": srt_url,
                    "has_audio": bool(mp3_files),
                    "has_transcript": has_transcript,
                    "has_description": has_description,
                }
        except Exception as e:
            if attempt < retries:
                time.sleep(attempt * 1.5)
            else:
                return {
                    "item_id": identifier,
                    "action": "ERROR",
                    "error": str(e),
                    "audio_url": None,
                    "srt_url": None,
                    "has_audio": False,
                    "has_transcript": False,
                    "has_description": False,
                }
    return {
        "item_id": identifier,
        "action": "ERROR",
        "error": "Timeout retries exhausted",
        "audio_url": None,
        "srt_url": None,
        "has_audio": False,
        "has_transcript": False,
        "has_description": False,
    }


def scan_pipeline(
    limit: int = 5,
    max_scan: int = 1000,
    item_id: Optional[str] = None,
    mode: str = "auto",
    workers: int = 15
) -> Tuple[List[Dict], Dict]:
    """
    Scan rekaman dan kembalikan daftar task yang belum lengkap sesuai limit & mode,
    beserta statistik agregat.
    """
    if item_id:
        log(f"Memeriksa identifier tunggal: {item_id}")
        item_info = check_item_status(item_id)
        stats = {
            "total": 1,
            "complete": 1 if item_info["action"] == "COMPLETE" else 0,
            "needs_desc": 1 if item_info["action"] == "describe_only" else 0,
            "needs_trans": 1 if item_info["action"] == "transcribe_and_describe" else 0,
            "no_audio": 1 if item_info["action"] == "IGNORE_NO_AUDIO" else 0,
            "error": 1 if item_info["action"] == "ERROR" else 0,
        }
        if item_info["action"] in ["transcribe_and_describe", "describe_only"]:
            return [item_info], stats
        elif item_info["action"] == "COMPLETE":
            log(f"Item {item_id} sudah lengkap (transcript & description.json sudah ada).")
            return [], stats
        else:
            log(f"Item {item_id} status: {item_info.get('action')}")
            return [], stats

    log(f"Mengambil {max_scan} rekaman terbaru dari Archive.org...")
    docs = fetch_archive_items(rows=max_scan)
    if not docs:
        log("Tidak ada item ditemukan.")
        return [], {"total": 0, "complete": 0, "needs_desc": 0, "needs_trans": 0, "no_audio": 0, "error": 0}

    identifiers = [d["identifier"] for d in docs if "identifier" in d]
    log(f"Memeriksa metadata {len(identifiers)} item secara paralel (workers={workers})...")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        statuses = list(executor.map(check_item_status, identifiers))

    transcribe_needed = []
    describe_needed = []
    complete_count = 0
    no_audio_count = 0
    error_count = 0

    for s in statuses:
        act = s.get("action")
        if act == "transcribe_and_describe":
            transcribe_needed.append(s)
        elif act == "describe_only":
            describe_needed.append(s)
        elif act == "COMPLETE":
            complete_count += 1
        elif act == "IGNORE_NO_AUDIO":
            no_audio_count += 1
        elif act == "ERROR":
            error_count += 1

    stats = {
        "total": len(statuses),
        "complete": complete_count,
        "needs_desc": len(describe_needed),
        "needs_trans": len(transcribe_needed),
        "no_audio": no_audio_count,
        "error": error_count,
    }

    log(f"Hasil scan ({len(statuses)} item): {complete_count} lengkap, "
        f"{len(transcribe_needed)} butuh transkrip, {len(describe_needed)} butuh deskripsi saja.")

    tasks = []
    if mode == "auto":
        # Prioritas transkripsi lebih dulu, lalu deskripsi
        tasks.extend(transcribe_needed)
        tasks.extend(describe_needed)
    elif mode == "describe_first":
        # Prioritas perbaikan cepat deskripsi lebih dulu
        tasks.extend(describe_needed)
        tasks.extend(transcribe_needed)
    elif mode == "transcribe_only":
        tasks.extend(transcribe_needed)
    elif mode == "describe_only":
        tasks.extend(describe_needed)
    elif mode == "balanced":
        # Selang-seling antara transcribe dan describe
        i, j = 0, 0
        while i < len(transcribe_needed) or j < len(describe_needed):
            if i < len(transcribe_needed):
                tasks.append(transcribe_needed[i])
                i += 1
            if j < len(describe_needed):
                tasks.append(describe_needed[j])
                j += 1

    # Batasi sesuai limit
    selected_tasks = tasks[:limit]
    log(f"Dipilih {len(selected_tasks)} task untuk dijalankan (limit={limit}, mode={mode}).")
    for idx, t in enumerate(selected_tasks, 1):
        log(f"  {idx}. [{t['action']}] {t['item_id']} -> {t.get('audio_url') or t.get('srt_url')}")

    return selected_tasks, stats


def generate_github_step_summary(tasks: List[Dict], stats: Optional[Dict] = None):
    """Tulis ringkasan scan dalam format Markdown ke $GITHUB_STEP_SUMMARY."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("## 🛡️ Pipeline Guard: Hasil Scan Arsip Archive.org\n\n")
            if stats:
                total = stats.get("total", 0)
                complete = stats.get("complete", 0)
                pct = (complete / total * 100) if total > 0 else 0
                f.write("| Kategori Status | Jumlah | Persentase |\n")
                f.write("|---|---:|---:|\n")
                f.write(f"| **Total Diperiksa** | **{total}** | 100% |\n")
                f.write(f"| ✅ Lengkap (Complete) | {complete} | {pct:.1f}% |\n")
                f.write(f"| 📝 Butuh Deskripsi Saja | {stats.get('needs_desc', 0)} | -\n")
                f.write(f"| 🎙️ Butuh Transkrip & Deskripsi | {stats.get('needs_trans', 0)} | -\n")
                f.write(f"| ⚠️ Tanpa Audio / Error | {stats.get('no_audio', 0) + stats.get('error', 0)} | -\n\n")

            if tasks:
                f.write(f"### 📋 Task Terpilih untuk Dieksekusi ({len(tasks)})\n\n")
                f.write("| No | Aksi | Archive Item | Sumber File |\n")
                f.write("|---|---|---|---|\n")
                for i, t in enumerate(tasks, 1):
                    act_badge = "🎙️ Transkrip & Deskripsi" if t["action"] == "transcribe_and_describe" else "📝 Deskripsi Saja"
                    item_link = f"[{t['item_id']}](https://archive.org/details/{t['item_id']})"
                    src = t.get("audio_url") or t.get("srt_url") or "-"
                    fname = os.path.basename(src) if src != "-" else "-"
                    f.write(f"| {i} | {act_badge} | {item_link} | `{fname}` |\n")
                f.write("\n")
            else:
                f.write("🎉 **Seluruh rekaman yang diperiksa dalam kondisi lengkap! Tidak ada task yang perlu dieksekusi.**\n\n")
        log("Berhasil menulis laporan ke GITHUB_STEP_SUMMARY.")
    except Exception as e:
        log(f"Gagal menulis GITHUB_STEP_SUMMARY: {e}")


def export_github_outputs(tasks: List[Dict], stats: Optional[Dict] = None):
    """
    Ekspor matriks dan status ke GITHUB_OUTPUT.
    Menghindari error runner 'matrix must evaluate to at least one job'
    dengan menyuplai dummy task yang diskip jika tasks kosong.
    """
    github_output = os.environ.get("GITHUB_OUTPUT")
    has_items = bool(tasks)

    # Safety wrapper: jika tasks kosong, berikan list dummy agar include: ${{ fromJson(...) }} tidak crash
    safe_matrix = tasks if has_items else [{"item_id": "skip", "action": "skip", "audio_url": "", "srt_url": ""}]
    matrix_json = json.dumps(safe_matrix)

    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"matrix={matrix_json}\n")
            f.write(f"count={len(tasks)}\n")
            f.write(f"has_items={'true' if has_items else 'false'}\n")
        log("Berhasil mengekspor output ke GITHUB_OUTPUT.")
    else:
        log("GITHUB_OUTPUT tidak terdeteksi, mencetak JSON matriks:")
        print(matrix_json)

    generate_github_step_summary(tasks, stats)


def print_status_dashboard(stats: Dict, max_scan: int, recent_items: List[Dict]):
    """Cetak status dashboard visual di terminal."""
    total = stats.get("total", 0)
    complete = stats.get("complete", 0)
    needs_desc = stats.get("needs_desc", 0)
    needs_trans = stats.get("needs_trans", 0)
    no_audio = stats.get("no_audio", 0)
    errors = stats.get("error", 0)
    pct = (complete / total * 100) if total > 0 else 0

    print("\n" + "=" * 65)
    print(" 🛡️  VOICE OF TRISMA - PIPELINE GUARD STATUS REPORT")
    print("=" * 65)
    print(f" Total Arsip Diperiksa    : {total} (Maksimum sampel: {max_scan})")
    print(f" ✅ Lengkap (Complete)     : {complete:<4} ({pct:5.1f}%)")
    print(f" 📝 Butuh Deskripsi Saja   : {needs_desc:<4}")
    print(f" 🎙️ Butuh Transkrip Penuh : {needs_trans:<4}")
    if no_audio > 0:
        print(f" ⚠️  Tanpa Audio MP3       : {no_audio:<4}")
    if errors > 0:
        print(f" ❌ Error Metadata         : {errors:<4}")
    print("-" * 65)

    if recent_items:
        print(" Sampel 10 Item Terbaru:")
        for idx, item in enumerate(recent_items[:10], 1):
            act = item.get("action")
            icon = "✅" if act == "COMPLETE" else ("📝" if act == "describe_only" else ("🎙️" if act == "transcribe_and_describe" else "⚠️"))
            print(f"  {idx:2d}. {icon} {item.get('item_id'):<32} [{act}]")
    print("=" * 65 + "\n")


def fix_description_item(
    item_id: str,
    srt_url: Optional[str] = None,
    model: Optional[str] = None,
    dry_run: bool = False
):
    """
    Unduh transkrip SRT yang sudah ada di Archive.org,
    jalankan descriptor.py untuk membuat description.json,
    lalu upload description.json via ia.py ke item yang bersangkutan.
    """
    log(f"Memulai pembuatan deskripsi untuk item: {item_id}")

    if not srt_url:
        srt_url = f"https://archive.org/download/{item_id}/transcript.srt"

    temp_dir = os.path.join("temp_guard", item_id)
    transcripts_dir = "transcripts"
    os.makedirs(temp_dir, exist_ok=True)
    os.makedirs(transcripts_dir, exist_ok=True)

    srt_path = os.path.join(temp_dir, "transcript.srt")
    out_desc_path = os.path.join(transcripts_dir, "description.json")

    def get_archive_proxy_url(u: str) -> Optional[str]:
        archive_prefix = "https://archive.org/download/"
        if u and u.startswith(archive_prefix):
            subpath = u[len(archive_prefix):]
            proxy_base = os.environ.get("CF_WORKER_PROXY_URL") or "https://voiceoftrisma.anandapradnyana68.workers.dev/archive/download"
            return f"{proxy_base.rstrip('/')}/{subpath}"
        return None

    try:
        download_candidates = []
        proxy_srt = get_archive_proxy_url(srt_url)
        if proxy_srt:
            download_candidates.append(proxy_srt)
        download_candidates.append(srt_url)

        downloaded = False
        for u in download_candidates:
            try:
                log(f"Mengunduh transkrip dari: {u}")
                req = urllib.request.Request(u, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=30) as resp, open(srt_path, "wb") as out_file:
                    shutil.copyfileobj(resp, out_file)
                log(f"Transkrip berhasil diunduh ke: {srt_path} ({os.path.getsize(srt_path)} bytes)")
                downloaded = True
                break
            except Exception as e:
                log(f"[WARN] Gagal mengunduh {u}: {e}")

        if not downloaded:
            txt_url = f"https://archive.org/download/{item_id}/transcript.txt"
            proxy_txt = get_archive_proxy_url(txt_url)
            txt_candidates = [proxy_txt, txt_url] if proxy_txt else [txt_url]
            for tu in txt_candidates:
                try:
                    log(f"Mencoba alternatif: {tu}")
                    req_txt = urllib.request.Request(tu, headers={"User-Agent": USER_AGENT})
                    with urllib.request.urlopen(req_txt, timeout=30) as resp, open(srt_path, "wb") as out_file:
                        shutil.copyfileobj(resp, out_file)
                    log(f"Transcript TXT berhasil diunduh ke: {srt_path}")
                    downloaded = True
                    break
                except Exception as e2:
                    log(f"[WARN] Gagal mengunduh alternatif {tu}: {e2}")

        if not downloaded:
            log(f"[FATAL] Gagal mengunduh transkrip untuk {item_id}.")
            sys.exit(1)

        if dry_run:
            log("[DRY-RUN] Melewati eksekusi Gemini descriptor dan upload.")
            return

        # Bersihkan folder transcripts agar HANYA berisi description.json baru
        for f in os.listdir(transcripts_dir):
            fp = os.path.join(transcripts_dir, f)
            if os.path.isfile(fp):
                os.remove(fp)

        # Jalankan descriptor.py
        log(f"Menjalankan descriptor.py dengan input: {srt_path}")
        import subprocess
        cmd_desc = [
            sys.executable,
            "main/descriptor.py",
            srt_path,
            "-o",
            out_desc_path
        ]
        if model:
            cmd_desc.extend(["-m", model])

        res = subprocess.run(cmd_desc)
        if res.returncode != 0:
            log("[ERROR] descriptor.py gagal membuat description.json!")
            sys.exit(res.returncode)

        if not os.path.exists(out_desc_path):
            log(f"[ERROR] File {out_desc_path} tidak ditemukan setelah eksekusi descriptor.py!")
            sys.exit(1)

        log(f"[OK] description.json berhasil dibuat ({os.path.getsize(out_desc_path)} bytes).")

        # Upload ke Archive.org via main/archive.py
        log(f"Mengupload description.json ke item Archive.org: {item_id}")
        env = os.environ.copy()
        env["ITEM_ID"] = item_id
        cmd_upload = [sys.executable, "-u", "main/archive.py"]
        res_upload = subprocess.run(cmd_upload, env=env)
        if res_upload.returncode != 0:
            log("[ERROR] archive.py gagal mengupload description.json!")
            sys.exit(res_upload.returncode)

        log(f"[SUCCESS] description.json untuk {item_id} berhasil diupload ke Archive.org.")
    finally:
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Pipeline Guard CLI for Voice of Trisma")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: scan
    scan_parser = subparsers.add_parser("scan", help="Scan rekaman Archive.org untuk task matriks GitHub Actions")
    scan_parser.add_argument("--limit", type=int, default=5, help="Jumlah maksimal item yang diproses")
    scan_parser.add_argument("--max-scan", type=int, default=1000, help="Jumlah rekaman terbaru yang diperiksa (default: 1000)")
    scan_parser.add_argument("--item-id", type=str, default=None, help="Identifier spesifik yang ingin diperiksa")
    scan_parser.add_argument(
        "--mode",
        choices=["auto", "describe_first", "transcribe_only", "describe_only", "balanced"],
        default="auto",
        help="Strategi pemilihan task"
    )
    scan_parser.add_argument("--output-json", type=str, default=None, help="Path file untuk menyimpan hasil JSON matriks")
    scan_parser.add_argument("--export-github-output", action="store_true", help="Format output untuk GITHUB_OUTPUT environment")

    # Subcommand: status
    status_parser = subparsers.add_parser("status", help="Tampilkan laporan kesehatan arsip (dashboard ringkasan)")
    status_parser.add_argument("--max-scan", type=int, default=1000, help="Jumlah rekaman terbaru yang diperiksa (default: 1000)")
    status_parser.add_argument("--json", action="store_true", help="Keluarkan hasil dalam format JSON murni")

    # Subcommand: fix-description
    fix_parser = subparsers.add_parser("fix-description", help="Buat dan upload description.json untuk item yang sudah punya transkrip")
    fix_parser.add_argument("--item-id", type=str, required=True, help="Identifier Archive.org")
    fix_parser.add_argument("--srt-url", type=str, default=None, help="URL download transcript.srt")
    fix_parser.add_argument("--model", type=str, default=None, help="Nama model Gemini yang diprioritaskan")
    fix_parser.add_argument("--dry-run", action="store_true", help="Uji coba tanpa memanggil Gemini dan upload")

    args = parser.parse_args()

    if args.command == "scan":
        tasks, stats = scan_pipeline(
            limit=args.limit,
            max_scan=args.max_scan,
            item_id=args.item_id,
            mode=args.mode
        )
        matrix_json = json.dumps(tasks)

        if args.output_json:
            with open(args.output_json, "w", encoding="utf-8") as f:
                f.write(matrix_json)
            log(f"Matriks tersimpan di: {args.output_json}")

        if args.export_github_output:
            export_github_outputs(tasks, stats)
        else:
            print(matrix_json)

    elif args.command == "status":
        _, stats = scan_pipeline(limit=0, max_scan=args.max_scan, mode="auto")
        docs = fetch_archive_items(rows=min(args.max_scan, 10))
        recent_ids = [d["identifier"] for d in docs if "identifier" in d]
        with ThreadPoolExecutor(max_workers=10) as executor:
            recent_items = list(executor.map(check_item_status, recent_ids))

        if args.json:
            result = {
                "stats": stats,
                "recent_items": recent_items
            }
            print(json.dumps(result, indent=2))
        else:
            print_status_dashboard(stats, args.max_scan, recent_items)

    elif args.command == "fix-description":
        fix_description_item(
            item_id=args.item_id,
            srt_url=args.srt_url,
            model=args.model,
            dry_run=args.dry_run
        )


if __name__ == "__main__":
    main()
