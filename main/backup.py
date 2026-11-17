#!/usr/bin/env python3
"""
Voice of Trisma — Auto-Backup Archive.org ke Hugging Face Dataset.

Mengunduh seluruh berkas dari setiap item identifier Archive.org
(audio, transkrip, deskripsi, metadata, torrent, sqlite, dsb.) secara paralel,
lalu mengunggahnya per batch (default 100 identifier) ke Hugging Face Hub Dataset:
https://huggingface.co/datasets/Sparkplugx1904/voiceoftrisma

Format penyimpanan di HF:
records/{identifier}/{files}
"""

import os
import sys
import time
import shutil
import urllib.request
import urllib.parse
import json
import argparse
from pathlib import Path
from typing import List, Dict, Set, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

_MAIN_DIR = Path(__file__).resolve().parent
if str(_MAIN_DIR) not in sys.path:
    sys.path.insert(0, str(_MAIN_DIR))

# Muat variabel .ENV root
try:
    from core.config import ROOT_DIR, USER_AGENT
except ImportError:
    try:
        from main.core.config import ROOT_DIR, USER_AGENT
    except ImportError:
        ROOT_DIR = Path(__file__).resolve().parent.parent
        USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# Load .ENV jika ada
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

try:
    from huggingface_hub import HfApi
except ImportError:
    print("[FATAL] Pustaka 'huggingface_hub' belum terpasang. Jalankan 'pip install huggingface_hub'.", file=sys.stderr)
    sys.exit(1)

DEFAULT_REPO_ID = "Sparkplugx1904/voiceoftrisma"
ARCHIVE_SEARCH_URL = (
    "https://archive.org/advancedsearch.php?"
    "q=identifier:vot-denpasar-*&fl[]=identifier,publicdate"
    "&sort[]=publicdate+desc&rows={rows}&output=json"
)
ARCHIVE_METADATA_FILES_URL = "https://archive.org/metadata/{identifier}/files"


def log(msg: str):
    """Mencatat log dengan timestamp."""
    now = time.strftime("%H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def format_size(bytes_val: int) -> str:
    """Format bytes ke string manusiawi (B, KB, MB, GB)."""
    val = float(bytes_val)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if val < 1024.0:
            return f"{val:.2f} {unit}"
        val /= 1024.0
    return f"{val:.2f} PB"


def get_archive_identifiers(query_rows: int = 3000, retries: int = 3) -> List[str]:
    """Mengambil seluruh daftar identifier vot-denpasar-* dari Archive.org."""
    url = ARCHIVE_SEARCH_URL.format(rows=query_rows)
    log(f"Mengambil daftar identifier dari Archive.org (rows={query_rows})...")
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                docs = data.get("response", {}).get("docs", [])
                identifiers = [doc["identifier"] for doc in docs if doc.get("identifier")]
                log(f"Ditemukan {len(identifiers)} identifier di Archive.org.")
                return identifiers
        except Exception as e:
            if attempt < retries:
                time.sleep(attempt * 2)
            else:
                log(f"[ERROR] Gagal mengambil daftar identifier dari Archive.org: {e}")
                return []
    return []


def get_archive_files_for_identifier(identifier: str, retries: int = 3) -> List[str]:
    """Mengambil daftar seluruh berkas di dalam 1 item identifier Archive.org."""
    url = ARCHIVE_METADATA_FILES_URL.format(identifier=identifier)
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                files_raw = data.get("result", [])
                file_names = [f["name"] for f in files_raw if f.get("name")]
                return file_names
        except Exception as e:
            if attempt < retries:
                time.sleep(attempt * 1.5)
            else:
                log(f"[WARN] Gagal mengambil daftar berkas untuk '{identifier}': {e}")
                return []
    return []


def get_existing_hf_identifiers(api: HfApi, repo_id: str) -> Set[str]:
    """Mengambil daftar identifier yang sudah ada di Hugging Face dataset (folder records/)."""
    log(f"Memeriksa arsip yang sudah ada di Hugging Face dataset '{repo_id}'...")
    try:
        files = api.list_repo_files(repo_id=repo_id, repo_type="dataset")
        existing: Set[str] = set()
        for f in files:
            if f.startswith("records/"):
                parts = f.split("/")
                if len(parts) >= 2 and parts[1]:
                    existing.add(parts[1])
        log(f"Ditemukan {len(existing)} identifier yang sudah tersimpan di Hugging Face.")
        return existing
    except Exception as e:
        log(f"[INFO] Belum dapat membaca struktur records/ di HF ({e}), menganggap kosong.")
        return set()


def download_single_file(
    identifier: str,
    file_name: str,
    dest_path: Path,
    retries: int = 3,
    timeout: int = 180
) -> Tuple[bool, int, str]:
    """
    Mengunduh satu berkas dari Archive.org secara streaming ke file .tmp
    lalu me-rename secara atomic setelah selesai.
    """
    quoted_name = urllib.parse.quote(file_name, safe="/")
    url = f"https://archive.org/download/{identifier}/{quoted_name}"
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest_path.with_name(f"{dest_path.name}.tmp")

    # Jika file sudah ada dan ukuran > 0, lewati
    if dest_path.is_file() and dest_path.stat().st_size > 0:
        return True, dest_path.stat().st_size, ""

    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    time.sleep(attempt * 2)
                    continue

                total_bytes = 0
                with open(temp_path, "wb") as f:
                    while True:
                        chunk = resp.read(1024 * 1024)  # 1MB chunk
                        if not chunk:
                            break
                        f.write(chunk)
                        total_bytes += len(chunk)

            if temp_path.exists():
                temp_path.replace(dest_path)
            return True, total_bytes, ""

        except Exception as e:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass
            if attempt < retries:
                time.sleep(attempt * 2)
            else:
                return False, 0, f"{file_name}: {e}"

    return False, 0, f"{file_name}: timeout/gagal setelah {retries}x percobaan"


def backup_batch(
    api: HfApi,
    repo_id: str,
    batch_identifiers: List[str],
    staging_dir: Path,
    batch_idx: int,
    total_batches: int,
    workers: int = 10,
) -> Dict:
    """
    Memproses 1 batch identifier:
    1. Ambil list semua file per identifier secara paralel.
    2. Unduh seluruh file secara paralel ke staging_dir/records/{identifier}/{files}.
    3. Upload folder staging_dir/records ke Hugging Face Hub Dataset.
    4. Bersihkan staging_dir untuk menghemat ruang disk runner.
    """
    batch_len = len(batch_identifiers)
    batch_records_dir = staging_dir / "records"
    if batch_records_dir.exists():
        shutil.rmtree(batch_records_dir)
    batch_records_dir.mkdir(parents=True, exist_ok=True)

    log(f"============================================================")
    log(f"🚀 Memulai Batch {batch_idx}/{total_batches} ({batch_len} identifier)")
    log(f"   Rentang: {batch_identifiers[0]} .. {batch_identifiers[-1]}")
    log(f"============================================================")

    # 1. Kumpulkan seluruh berkas untuk setiap identifier di batch
    log(f"Mengumpulkan metadata daftar berkas untuk {batch_len} identifier...")
    identifier_files_map: Dict[str, List[str]] = {}

    with ThreadPoolExecutor(max_workers=min(workers, 16)) as executor:
        future_to_id = {
            executor.submit(get_archive_files_for_identifier, ident): ident
            for ident in batch_identifiers
        }
        for future in as_completed(future_to_id):
            ident = future_to_id[future]
            try:
                flist = future.result()
                if flist:
                    identifier_files_map[ident] = flist
                else:
                    log(f"[WARN] Identifier '{ident}' tidak mengembalikan daftar berkas.")
            except Exception as e:
                log(f"[ERROR] Gagal memproses metadata '{ident}': {e}")

    # Buat daftar task unduhan
    download_tasks: List[Tuple[str, str, Path]] = []
    for ident, files in identifier_files_map.items():
        for fname in files:
            target_path = batch_records_dir / ident / fname
            download_tasks.append((ident, fname, target_path))

    total_tasks = len(download_tasks)
    log(f"Total {total_tasks} berkas siap diunduh secara paralel (workers={workers})...")

    # 2. Unduh berkas secara paralel
    t0 = time.time()
    success_files = 0
    failed_files = 0
    total_bytes = 0
    errors: List[str] = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_task = {
            executor.submit(download_single_file, ident, fname, target): (ident, fname)
            for ident, fname, target in download_tasks
        }

        completed_count = 0
        for future in as_completed(future_to_task):
            completed_count += 1
            ident, fname = future_to_task[future]
            try:
                ok, nbytes, err_msg = future.result()
                if ok:
                    success_files += 1
                    total_bytes += nbytes
                else:
                    failed_files += 1
                    errors.append(f"[{ident}] {err_msg}")
            except Exception as e:
                failed_files += 1
                errors.append(f"[{ident}] {fname}: {e}")

            if completed_count % 50 == 0 or completed_count == total_tasks:
                pct = (completed_count / total_tasks) * 100
                log(f"  [Unduh] {completed_count}/{total_tasks} berkas ({pct:.1f}%) | "
                    f"Terkumpul: {format_size(total_bytes)}")

    download_duration = time.time() - t0
    log(f"✓ Selesai mengunduh Batch {batch_idx}: {success_files} sukses, {failed_files} gagal "
        f"({format_size(total_bytes)}) dalam {download_duration:.1f} detik.")

    if failed_files > 0 and len(errors) <= 10:
        for err in errors:
            log(f"  · {err}")
    elif failed_files > 10:
        log(f"  · Terjadi {failed_files} kegagalan berkas (menampilkan 5 pertama):")
        for err in errors[:5]:
            log(f"    · {err}")

    # 3. Unggah folder ke Hugging Face Hub Dataset
    t_up = time.time()
    commit_msg = (
        f"backup: batch {batch_idx}/{total_batches} "
        f"({batch_len} identifiers: {batch_identifiers[0]} .. {batch_identifiers[-1]})"
    )
    log(f"Mengunggah ke Hugging Face dataset '{repo_id}' (path_in_repo='records')...")

    try:
        api.upload_folder(
            repo_id=repo_id,
            folder_path=str(batch_records_dir),
            path_in_repo="records",
            repo_type="dataset",
            commit_message=commit_msg,
        )
        upload_duration = time.time() - t_up
        log(f"✅ Berhasil mengunggah Batch {batch_idx}/{total_batches} ke Hugging Face "
            f"dalam {upload_duration:.1f} detik.")
        upload_success = True
    except Exception as e:
        log(f"[FATAL] Gagal mengunggah Batch {batch_idx} ke Hugging Face: {e}")
        upload_success = False

    # 4. Bersihkan direktori staging lokal untuk menjaga ruang disk runner
    try:
        shutil.rmtree(batch_records_dir)
        log(f"Pembersihan disk lokal: {batch_records_dir} berhasil dihapus.")
    except Exception as e:
        log(f"[WARN] Gagal membersihkan direktori {batch_records_dir}: {e}")

    return {
        "batch_idx": batch_idx,
        "identifiers_count": batch_len,
        "total_files": total_tasks,
        "success_files": success_files,
        "failed_files": failed_files,
        "total_bytes": total_bytes,
        "download_duration": download_duration,
        "upload_success": upload_success,
    }


def write_summary(
    summary_path: Path,
    repo_id: str,
    total_found: int,
    already_backed_up: int,
    processed_count: int,
    batch_results: List[Dict],
):
    """Menulis ringkasan hasil backup dalam format Markdown untuk GitHub Step Summary."""
    total_files_all = sum(b.get("success_files", 0) for b in batch_results)
    total_bytes_all = sum(b.get("total_bytes", 0) for b in batch_results)
    successful_batches = sum(1 for b in batch_results if b.get("upload_success", False))

    lines = [
        "## 📦 Voice of Trisma — Auto Backup ke Hugging Face Dataset",
        "",
        f"- **Dataset Tujuan**: [`{repo_id}`](https://huggingface.co/datasets/{repo_id})",
        f"- **Total Identifier di Archive.org**: `{total_found}`",
        f"- **Identifier Sudah Ada Sebelumnya**: `{already_backed_up}`",
        f"- **Identifier Baru yang Diproses**: `{processed_count}`",
        f"- **Total Berkas Terunggah**: `{total_files_all}`",
        f"- **Total Volume Data**: `{format_size(total_bytes_all)}`",
        f"- **Status Batch**: `{successful_batches}/{len(batch_results)}` batch berhasil diunggah",
        "",
        "### Rincian Per Batch",
        "",
        "| Batch | Identifier | Berkas Sukses | Berkas Gagal | Ukuran | Status Upload |",
        "| :---: | :---: | :---: | :---: | :---: | :---: |",
    ]

    for b in batch_results:
        status_icon = "✅ Sukses" if b.get("upload_success") else "❌ Gagal"
        lines.append(
            f"| #{b['batch_idx']} | {b['identifiers_count']} | {b['success_files']} | "
            f"{b['failed_files']} | {format_size(b['total_bytes'])} | {status_icon} |"
        )

    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    log(f"Ringkasan eksekusi ditulis ke {summary_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Auto-backup seluruh berkas Archive.org Voice of Trisma ke Hugging Face Dataset.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("HUGGINGFACE_TOKEN"),
        help="Hugging Face User Access Token (atau via env HUGGINGFACE_TOKEN).",
    )
    parser.add_argument(
        "--repo-id",
        default=DEFAULT_REPO_ID,
        help=f"Dataset repository ID di Hugging Face. Default: {DEFAULT_REPO_ID}",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Jumlah identifier yang diunduh dan diunggah per batch. Default: 100",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=0,
        help="Batas maksimal batch yang diproses dalam 1 run (0 = semua hingga selesai). Default: 0",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=10,
        help="Jumlah thread paralel untuk mengunduh berkas. Default: 10",
    )
    parser.add_argument(
        "--identifier",
        default=None,
        help="Identifier tunggal spesifik jika hanya ingin mem-backup 1 rekaman tertentu.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Unduh dan unggah ulang meskipun identifier sudah ada di Hugging Face.",
    )
    parser.add_argument(
        "--staging-dir",
        default="backup_staging",
        help="Direktori penyimpanan lokal sementara. Default: 'backup_staging'",
    )
    parser.add_argument(
        "--export-summary",
        default=None,
        help="Path file output untuk menulis ringkasan Markdown (misal: $GITHUB_STEP_SUMMARY).",
    )

    args = parser.parse_args()

    # Validasi & sanitasi token HF (bersihkan whitespace, \r, \n, dan tanda kutip)
    raw_token = args.token or os.environ.get("HUGGINGFACE_TOKEN", "")
    token = raw_token.strip().strip("'\"").strip() if raw_token else ""
    if not token:
        log("[FATAL] HUGGINGFACE_TOKEN tidak ditemukan di argumen maupun environment variable!")
        sys.exit(1)

    api = HfApi(token=token)

    # Validasi awal token & akses ke Hugging Face (fail-fast sebelum mengunduh data besar)
    try:
        user_info = api.whoami()
        user_name = user_info.get("name") or user_info.get("fullname") or "authenticated_user"
        log(f"✓ Berhasil terautentikasi ke Hugging Face sebagai '{user_name}'.")
    except Exception as e:
        log(f"[FATAL] Gagal autentikasi ke Hugging Face dengan token yang diberikan: {e}")
        sys.exit(1)

    # 1. Dapatkan daftar identifier
    if args.identifier:
        all_identifiers = [args.identifier.strip()]
        log(f"Mode identifier tunggal: {all_identifiers[0]}")
    else:
        all_identifiers = get_archive_identifiers()

    if not all_identifiers:
        log("[WARN] Tidak ada identifier yang ditemukan di Archive.org. Keluar.")
        sys.exit(0)

    # 2. Cek identifier yang sudah dicadangkan di Hugging Face
    existing_hf = set()
    if not args.force:
        existing_hf = get_existing_hf_identifiers(api, args.repo_id)

    pending_identifiers = [i for i in all_identifiers if i not in existing_hf]
    total_found = len(all_identifiers)
    already_backed_up = len(existing_hf)
    log(f"📊 Status Arsip:")
    log(f"   - Total di Archive.org : {total_found}")
    log(f"   - Sudah ada di HF      : {len(all_identifiers) - len(pending_identifiers)}")
    log(f"   - Perlu dicadangkan    : {len(pending_identifiers)}")

    if not pending_identifiers:
        log("🎉 Semua identifier sudah tersimpan di Hugging Face Dataset! Tidak ada pekerjaan baru.")
        if args.export_summary or os.environ.get("GITHUB_STEP_SUMMARY"):
            summary_target = Path(args.export_summary or os.environ["GITHUB_STEP_SUMMARY"])
            write_summary(summary_target, args.repo_id, total_found, already_backed_up, 0, [])
        sys.exit(0)

    # 3. Pecah ke dalam batches per 100 identifier
    batch_size = max(1, args.batch_size)
    batches = [
        pending_identifiers[i : i + batch_size]
        for i in range(0, len(pending_identifiers), batch_size)
    ]
    total_batches = len(batches)

    if args.max_batches > 0 and args.max_batches < total_batches:
        log(f"Membatasi eksekusi ke {args.max_batches} batch pertama (dari total {total_batches} batch).")
        batches = batches[: args.max_batches]
        total_batches = len(batches)

    log(f"Membagi {len(pending_identifiers)} identifier menjadi {total_batches} batch "
        f"(ukuran: ~{batch_size} identifier/batch).")

    staging_dir = Path(args.staging_dir).resolve()
    staging_dir.mkdir(parents=True, exist_ok=True)

    batch_results: List[Dict] = []

    try:
        for idx, batch_idents in enumerate(batches, start=1):
            res = backup_batch(
                api=api,
                repo_id=args.repo_id,
                batch_identifiers=batch_idents,
                staging_dir=staging_dir,
                batch_idx=idx,
                total_batches=total_batches,
                workers=args.workers,
            )
            batch_results.append(res)
            if not res.get("upload_success"):
                log(f"[FATAL] Menghentikan rangkaian backup karena Batch {idx} gagal diunggah.")
                break

    finally:
        # Pembersihan direktori staging induk
        if staging_dir.exists():
            try:
                shutil.rmtree(staging_dir)
            except Exception:
                pass

    # 4. Tulis ringkasan jika diminta
    summary_file = args.export_summary or os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        write_summary(
            Path(summary_file),
            args.repo_id,
            total_found,
            already_backed_up,
            len(pending_identifiers),
            batch_results,
        )

    # Validasi hasil akhir (fail-fast)
    has_failed_batches = any(not b.get("upload_success") for b in batch_results)
    if has_failed_batches:
        log("[FATAL] Auto-backup berakhir dengan kegagalan pada proses unggah.")
        sys.exit(1)

    log("🎉 Seluruh rangkaian auto-backup selesai dengan sukses.")


if __name__ == "__main__":
    main()
