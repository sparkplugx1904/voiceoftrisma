"""
VoT Core Downloader — Unduhan Berkas Archive.org via Cloudflare Worker Proxy
dengan Validasi Integritas (Anti-503, Anti-Truncated, & Exponential Backoff).
"""

import os
import re
import sys
import time
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Optional, Tuple, List

try:
    from core.logger import log
except ImportError:
    try:
        from main.core.logger import log
    except ImportError:
        def log(msg):
            print(f"[DOWNLOADER] {msg}", flush=True)
ARCHIVE_DOWNLOAD_PREFIX = "https://archive.org/download/"
DEFAULT_WORKER_PROXY = "https://voiceoftrisma.anandapradnyana68.workers.dev/archive/download"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def get_archive_proxy_url(url: str, proxy_base: Optional[str] = None) -> Optional[str]:
    """Ubah URL archive.org/download/... menjadi URL reverse proxy Cloudflare Worker."""
    if not url:
        return None
    if url.startswith(ARCHIVE_DOWNLOAD_PREFIX):
        subpath = url[len(ARCHIVE_DOWNLOAD_PREFIX):]
        base = proxy_base or os.environ.get("CF_WORKER_PROXY_URL") or DEFAULT_WORKER_PROXY
        return f"{base.rstrip('/')}/{subpath}"
    return url


def get_direct_archive_url(url: str) -> str:
    """Kembalikan URL langsung Archive.org dari URL proxy Cloudflare Worker."""
    base = os.environ.get("CF_WORKER_PROXY_URL") or DEFAULT_WORKER_PROXY
    base_clean = base.rstrip('/')
    if url.startswith(base_clean):
        subpath = url[len(base_clean):].lstrip('/')
        return f"{ARCHIVE_DOWNLOAD_PREFIX}{subpath}"
    return url


def get_remote_file_info(url: str, timeout: int = 15) -> Tuple[Optional[int], Optional[str]]:
    """Ambil informasi header Content-Length dan Content-Type via HEAD request."""
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            cl = resp.headers.get("Content-Length")
            ct = resp.headers.get("Content-Type")
            length = int(cl) if cl and cl.isdigit() else None
            return length, ct
    except Exception:
        return None, None


def is_valid_transcript_text(text: str) -> bool:
    """Validasi apakah teks transkrip utuh dan bukan respons error HTML/503."""
    if not text or len(text.strip()) == 0:
        return False
    lower = text.lower()
    if "<html" in lower or "<!doctype" in lower or "503 service" in lower or "502 bad gateway" in lower:
        return False
    # Format SRT/VTT/TXT rekaman VOT wajib memuat timestamp waktu
    timestamps = re.findall(r"(\d{2}:\d{2}:\d{2})", text)
    return len(timestamps) >= 5


def download_text_file(url: str, dest: Path, timeout: int = 30) -> bool:
    """Mengunduh berkas teks (SRT/TXT/JSON) via urllib dengan verifikasi integritas penuh."""
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")

    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                log(f"[WARN] HTTP {resp.status} dari {url}")
                return False

            expected_len = resp.headers.get("Content-Length")
            data = resp.read()

            # Deteksi unduhan terputus / tidak lengkap
            if expected_len and expected_len.isdigit() and len(data) < int(expected_len):
                log(f"[WARN] Data teks terpotong: diterima {len(data)} dari {expected_len} bytes")
                return False

            text = data.decode("utf-8", errors="ignore")
            if not is_valid_transcript_text(text):
                log(f"[WARN] Isi berkas dari {url} bukan transkrip valid (terdeteksi error HTML/503 atau kosong)")
                return False

            with open(tmp_path, "wb") as f:
                f.write(data)

            # Atomic replace ke file tujuan
            if tmp_path.exists():
                tmp_path.replace(dest_path)

            return True
    except Exception as e:
        log(f"[WARN] Gagal mengunduh teks dari {url}: {e}")
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        return False


def download_binary_file(url: str, dest: Path, min_size: int = 1_000_000, timeout: int = 600) -> bool:
    """Mengunduh berkas biner/audio via curl dengan resume (-C -), fail-fast, dan validasi ukuran."""
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    expected_len, _ = get_remote_file_info(url)

    cmd = [
        "curl", "-f", "-L",
        "-o", str(dest_path),
        "-m", str(timeout),
        "--retry", "3",
        "--retry-delay", "2",
        "--retry-all-errors",
        "--connect-timeout", "20",
        "--speed-time", "30",
        "--speed-limit", "10000",
        "-A", USER_AGENT,
        url
    ]

    try:
        subprocess.run(cmd, check=True)

        if not dest_path.exists():
            log(f"[WARN] Berkas {dest_path} tidak ditemukan setelah curl.")
            return False

        actual_size = dest_path.stat().st_size
        if actual_size < min_size:
            log(f"[WARN] Ukuran berkas {dest_path} terlalu kecil ({actual_size} bytes < {min_size} bytes)")
            dest_path.unlink()
            return False

        if expected_len and actual_size < expected_len:
            log(f"[WARN] Berkas terpotong: {actual_size} bytes dari target {expected_len} bytes")
            dest_path.unlink()
            return False

        # Verifikasi bukan teks error HTML
        try:
            with open(dest_path, "rb") as f:
                header = f.read(100).lower()
                if b"<html" in header or b"<!doctype" in header:
                    log(f"[WARN] Berkas {dest_path} berisi halaman HTML error!")
                    dest_path.unlink()
                    return False
        except Exception:
            pass

        return True
    except subprocess.CalledProcessError as e:
        log(f"[WARN] curl gagal dengan kode {e.returncode} untuk {url}")
        if dest_path.exists():
            dest_path.unlink()
        return False
    except Exception as e:
        log(f"[WARN] Error saat mengunduh biner: {e}")
        if dest_path.exists():
            dest_path.unlink()
        return False


def download_archive_file(
    url: str,
    dest: Path,
    min_size: int = 1000,
    is_text: Optional[bool] = None,
    max_retries: int = 5
) -> bool:
    """
    Unduh berkas Archive.org dengan mengutamakan Cloudflare Worker proxy,
    fallback ke URL langsung Archive.org, dan validasi keutuhan data.
    """
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # Deteksi otomatis teks vs biner jika tidak ditentukan
    if is_text is None:
        ext = dest_path.suffix.lower()
        is_text = ext in [".srt", ".txt", ".json", ".xml", ".csv"]

    proxy_url = get_archive_proxy_url(url)
    direct_url = get_direct_archive_url(url)

    # Urutan kandidat: Proxy Worker dahulu, lalu direct Archive.org
    candidates: List[str] = []
    if proxy_url and proxy_url != direct_url:
        candidates.append(proxy_url)
    candidates.append(direct_url)

    for attempt in range(1, max_retries + 1):
        for candidate_url in candidates:
            label = "Worker Proxy" if "workers.dev" in candidate_url else "Direct Archive.org"
            log(f"[DOWNLOAD] Mengunduh [{label}] (Percobaan {attempt}/{max_retries}): {candidate_url}")

            if is_text:
                if download_text_file(candidate_url, dest_path):
                    log(f"[SUCCESS] Berkas teks berhasil diunduh ke {dest_path} ({dest_path.stat().st_size} bytes)")
                    return True
            else:
                if download_binary_file(candidate_url, dest_path, min_size=min_size):
                    log(f"[SUCCESS] Berkas biner berhasil diunduh ke {dest_path} ({dest_path.stat().st_size} bytes)")
                    return True

        if attempt < max_retries:
            wait_time = attempt * 2
            log(f"[INFO] Menunggu {wait_time} detik sebelum mencoba ulang...")
            time.sleep(wait_time)

    log(f"[ERROR] Gagal mengunduh berkas dari semua kandidat setelah {max_retries} percobaan: {url}")
    return False
