#!/usr/bin/env python3
"""
Voice of Trisma — Setup utility.

Menggantikan baris-baris curl/pip/apt di GitHub Actions workflows
dengan satu perintah Python.

Usage:
  python setup.py record              # Setup environment recording (ffmpeg + pip)
  python setup.py transcript           # Setup environment transkripsi
  python setup.py hybrid               # Setup record + transkripsi
  # Script rekaman (record_v1.py / record_v2.py)
"""

import argparse
import os
import shutil
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.error import HTTPError, URLError

GH_BASE = "https://github.com/sparkplugx1904/voiceoftrisma/raw/refs/heads/main"


# ── helpers ────────────────────────────────────────────────────────────────


def _format_size(n_bytes: int) -> str:
    """Format bytes → KB / MB / GB."""
    for unit in ("B", "KB", "MB", "GB"):
        if n_bytes < 1024:
            return f"{n_bytes:.1f}{unit}"
        n_bytes /= 1024
    return f"{n_bytes:.1f}TB"


def _progress_hook(label: str):
    """
    Return a reporthook closure untuk urlretrieve.

    Render::
        ↓ filename  ████████░░░░░░░░ 50%
    """
    last_pct = [0]

    def hook(block_num: int, block_size: int, total_size: int):
        if total_size <= 0:
            return
        pct = int(block_num * block_size * 100 / total_size)
        if pct == last_pct[0]:
            return
        last_pct[0] = pct
        filled = pct * 16 // 100
        bar = "█" * filled + "░" * (16 - filled)
        print(f"\r  ↓  {label}  {bar} {pct}%", end="", flush=True)
        if pct >= 100:
            print()

    return hook


def download(url: str, dest: str, max_retries: int = 3) -> None:
    """
    Download *url* ke *dest* dengan retry, progress bar, dan validasi.

    Raises RuntimeError jika gagal setelah semua percobaan atau file kosong.
    """
    for attempt in range(1, max_retries + 1):
        try:
            urllib.request.urlretrieve(url, dest, _progress_hook(dest))

            # Validasi file hasil download
            if not os.path.exists(dest):
                raise RuntimeError("file tidak ditemukan setelah download")
            size = os.path.getsize(dest)
            if size == 0:
                raise RuntimeError("file kosong (0 B) — kemungkinan gagal download")

            print(f"  ✓  {dest} ({_format_size(size)})")
            return

        except HTTPError as exc:
            reason = f"HTTP {exc.code} {exc.reason}"
            print(f"  ⚠  download {dest} attempt {attempt}/{max_retries} — {reason}")
        except URLError as exc:
            reason = f"Network error: {exc.reason}"
            print(f"  ⚠  download {dest} attempt {attempt}/{max_retries} — {reason}")
        except Exception as exc:
            reason = getattr(exc, "reason", str(exc))
            print(f"  ⚠  download {dest} attempt {attempt}/{max_retries} — {reason}")

        if attempt < max_retries:
            time.sleep(2)

    raise RuntimeError(
        f"Gagal download {dest} setelah {max_retries}× percobaan.\n"
        f"  URL: {url}\n"
        f"  Cek koneksi internet atau coba manual."
    )


def run(args: list[str], **kwargs):
    """Print dan jalankan command; exit pada failure."""
    print(f"  $  {' '.join(args)}")
    subprocess.check_call(args, **kwargs)


def is_real_binary(path: str | Path, min_size: int = 1000) -> bool:
    """Periksa apakah file ada, bukan file kosong, dan bukan Git LFS pointer text (anti Errno 8)."""
    p = Path(path)
    if not p.is_file() or p.stat().st_size < min_size:
        return False
    try:
        with open(p, "rb") as f:
            header = f.read(16)
            if header.startswith(b"version https://"):
                return False
    except Exception:
        return False
    return True


def install_ffmpeg_apt_fallback(max_retries: int = 2, timeout: int = 120) -> bool:
    """Fallback instalasi FFmpeg via apt jika static binary tidak tersedia, dengan safe dpkg lock handling."""
    try:
        subprocess.run(["sudo", "systemctl", "stop", "unattended-upgrades", "apt-daily.timer", "apt-daily-upgrade.timer"], check=False)
        subprocess.run(["sudo", "killall", "-9", "apt", "apt-get"], check=False)
        subprocess.run(["sudo", "rm", "-f", "/var/lib/apt/lists/lock", "/var/lib/dpkg/lock-frontend", "/var/lib/dpkg/lock"], check=False)
        subprocess.run(["sudo", "dpkg", "--configure", "-a"], check=False)
    except Exception:
        pass

    for attempt in range(1, max_retries + 1):
        print(f"  ·  apt-get install ffmpeg attempt {attempt}/{max_retries} ...")
        try:
            subprocess.run(["sudo", "apt-get", "update", "-qq"], timeout=timeout, check=False)
            subprocess.run(["sudo", "apt-get", "install", "-y", "--no-install-recommends", "ffmpeg"], timeout=timeout, check=True)
            if shutil.which("ffmpeg"):
                print("  ✓  ffmpeg berhasil dipasang via apt-get")
                return True
        except Exception as exc:
            print(f"  ⚠  apt-get install ffmpeg attempt {attempt} gagal: {exc}")

        if attempt < max_retries:
            time.sleep(3)

    return False


def ensure_ffmpeg() -> bool:
    """
    Pastikan biner static FFmpeg & FFprobe dari repositori tersedia dan tertambat ke OS PATH.
    Mendukung Linux & Windows, instan, kebal error dpkg lock dan tidak bergantung pada apt-get.
    """
    print("  ·  Memeriksa dependensi FFmpeg & FFprobe...")

    # 1. Cek apakah ffmpeg sudah tersedia di PATH sistem dan merupakan binary valid
    existing_ffmpeg = shutil.which("ffmpeg")
    if existing_ffmpeg and is_real_binary(existing_ffmpeg):
        print(f"  ✓  ffmpeg sudah tersedia di OS PATH: {existing_ffmpeg}")
        return True

    # 2. Cari static binary di repositori
    if os.name == "nt":
        candidates_ffmpeg = [
            Path("bin/win-x64/ffmpeg.exe"),
            Path("bin/ffmpeg.exe"),
            Path("ffmpeg.exe"),
        ]
        candidates_ffprobe = [
            Path("bin/win-x64/ffprobe.exe"),
            Path("bin/ffprobe.exe"),
            Path("ffprobe.exe"),
        ]
        platform_dir = "bin/win-x64"
        ext = ".exe"
    else:
        candidates_ffmpeg = [
            Path("bin/linux-x64/ffmpeg"),
            Path("bin/ffmpeg"),
            Path("ffmpeg"),
        ]
        candidates_ffprobe = [
            Path("bin/linux-x64/ffprobe"),
            Path("bin/ffprobe"),
            Path("ffprobe"),
        ]
        platform_dir = "bin/linux-x64"
        ext = ""

    ffmpeg_src = next((c for c in candidates_ffmpeg if is_real_binary(c)), None)
    ffprobe_src = next((c for c in candidates_ffprobe if is_real_binary(c)), None)

    # 3. Jika file lokal tidak ada atau pointer LFS, unduh static binary mandiri dari repo
    if not ffmpeg_src:
        print("  ·  Static FFmpeg lokal tidak ditemukan atau pointer LFS, mengunduh biner mandiri...")
        dest = f"ffmpeg{ext}"
        try:
            download(f"{GH_BASE}/{platform_dir}/ffmpeg{ext}", dest)
            if is_real_binary(dest):
                ffmpeg_src = Path(dest)
        except Exception as exc:
            print(f"  ⚠  Gagal mengunduh static ffmpeg: {exc}")

    if not ffprobe_src:
        dest = f"ffprobe{ext}"
        try:
            download(f"{GH_BASE}/{platform_dir}/ffprobe{ext}", dest)
            if is_real_binary(dest):
                ffprobe_src = Path(dest)
        except Exception:
            pass

    # 4. Tambatkan (tether) static binary ke sistem OS
    if ffmpeg_src and ffmpeg_src.exists():
        # Berikan hak akses eksekusi
        if os.name != "nt":
            os.chmod(ffmpeg_src, os.stat(ffmpeg_src).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
            if ffprobe_src and ffprobe_src.exists():
                os.chmod(ffprobe_src, os.stat(ffprobe_src).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        # Di Linux/Unix: Pasang ke /usr/local/bin (level sistem OS untuk whisper.cpp, shell & python)
        if os.name != "nt":
            for tool_name, src_file in (("ffmpeg", ffmpeg_src), ("ffprobe", ffprobe_src)):
                if not src_file or not src_file.exists():
                    continue
                dest_path = f"/usr/local/bin/{tool_name}"
                try:
                    subprocess.run(["sudo", "ln", "-sf", str(src_file.resolve()), dest_path], check=False)
                    subprocess.run(["sudo", "chmod", "+x", dest_path], check=False)
                except Exception as e:
                    print(f"  ·  Info symlink {tool_name} ke {dest_path}: {e}")

        # Pastikan juga ada ./ffmpeg di root CWD (untuk kompatibilitas record_v1 & record_v2)
        local_target = Path(f"ffmpeg{ext}")
        if ffmpeg_src.resolve() != local_target.resolve() and not local_target.exists():
            try:
                if os.name != "nt":
                    os.symlink(ffmpeg_src.resolve(), local_target)
                else:
                    shutil.copy2(ffmpeg_src, local_target)
            except Exception:
                pass

        # Tambahkan direktori biner ke PATH proses saat ini & GITHUB_PATH
        bin_dir = str(ffmpeg_src.resolve().parent)
        if bin_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"

        if "GITHUB_PATH" in os.environ:
            try:
                with open(os.environ["GITHUB_PATH"], "a", encoding="utf-8") as f:
                    f.write(f"{bin_dir}\n")
                    if os.name != "nt":
                        f.write("/usr/local/bin\n")
            except Exception:
                pass

        which_ffmpeg = shutil.which("ffmpeg")
        if which_ffmpeg:
            print(f"  ✓  Static FFmpeg berhasil ditambatkan ke OS PATH: {which_ffmpeg}")
            return True

    # 5. Fallback darurat ke apt-get jika static binary gagal total
    if os.name != "nt" and shutil.which("apt-get"):
        print("  ⚠  Static FFmpeg tidak berhasil ditambatkan, mencoba fallback apt-get...")
        return install_ffmpeg_apt_fallback()

    print("  ⚠  FFmpeg tidak berhasil ditambatkan.")
    return False


# ── record ─────────────────────────────────────────────────────────────────


def setup_record():
    """
    Setup untuk job "Record Stream and Upload".

    Tambatkan FFmpeg & FFprobe (dari repo/download), chmod +x, pip install requirements record.
    Script rekaman (record_v1.py / record_v2.py)
    """
    print("\n── Setup record ──\n")

    if not ensure_ffmpeg():
        print("\n❌  Record setup gagal: FFmpeg tidak tersedia.")
        sys.exit(1)

    # ── pip install (dari requirements/ di checkout repo) ──
    req = os.path.join(os.path.dirname(__file__), "..", "requirements", "record")
    if not os.path.exists(req):
        req = "requirements/record"
    try:
        run([sys.executable, "-m", "pip", "install", "-r", req])
    except subprocess.CalledProcessError:
        print(f"\n❌  pip install gagal untuk {req}.")
        sys.exit(1)

    print("\n✓  Setup record selesai.\n")


# ── transcript ─────────────────────────────────────────────────────────────

WHISPER_BINS = [
    "bench", "command", "lsp", "main", "quantize", "stream",
    "test-vad", "test-vad-full", "vad-speech-segments", "wchess",
    "whisper-bench", "whisper-cli", "whisper-command", "whisper-server",
    "whisper-stream", "whisper-talk-llama",
]


def setup_transcript():
    """
    Setup untuk job "Transcribe and Push to Archive".

    1. Tambatkan static FFmpeg dari repo ke OS PATH (/usr/local/bin & PATH)
    2. chmod +x binary Whisper-CPP di bin/
    3. pip install dependencies transkripsi
    4. Install aria2 jika ada apt (opsional)
    """
    print("\n── Setup transcript ──\n")

    # 1. FFmpeg static binary tethering
    ensure_ffmpeg()

    # 2. chmod +x semua binary Whisper-CPP di bin/
    for binfile in WHISPER_BINS:
        for base_dir in ("bin/linux-x64", "bin"):
            dest = os.path.join(base_dir, binfile)
            if os.path.exists(dest) and os.name != "nt":
                os.chmod(dest, os.stat(dest).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    # 3. pip install dependencies untuk transkripsi
    req = os.path.join(os.path.dirname(__file__), "..", "requirements", "transcript")
    if os.path.exists(req):
        run([sys.executable, "-m", "pip", "install", "-r", req])
    else:
        print(f"  ·  {req} not found, skipping pip install.")

    # 4. Install aria2 (opsional, jika belum terpasang dan apt tersedia)
    if os.name != "nt" and shutil.which("apt-get") and not shutil.which("aria2c"):
        try:
            run(["sudo", "apt-get", "install", "-y", "-qq", "aria2"])
        except Exception as e:
            print(f"  ·  Apt aria2 install skipped: {e}")

    print("\n✓  Setup transcript selesai.\n")

def setup_descript():
    """
    Setup untuk job "Generate Description / Descriptor" (Qwen LLM / Gemini fallback + Archive.org upload).

    1. Chmod binary llama-server, llama-cli di bin/linux-x64/
    2. Install dependencies via pip dari requirements/descript (internetarchive, requests, dll)
    3. Install apt tools (aria2, internetarchive) jika Linux
    """
    print("\n── Setup descript ──\n")

    # 1. Chmod binaries untuk inference server
    for binfile in ("llama-server", "llama-cli", "whisper-cli"):
        for base_dir in ("bin/linux-x64", "bin"):
            dest = os.path.join(base_dir, binfile)
            if os.path.exists(dest) and os.name != "nt":
                os.chmod(dest, os.stat(dest).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
                print(f"  ✓  chmod +x {dest}")

    # 2. pip install requirements/descript
    req = os.path.join(os.path.dirname(__file__), "..", "requirements", "descript")
    if not os.path.exists(req):
        req = "requirements/descript"
    if os.path.exists(req):
        try:
            run([sys.executable, "-m", "pip", "install", "-r", req])
        except subprocess.CalledProcessError:
            print(f"\n❌  pip install gagal untuk {req}.")
            sys.exit(1)
    else:
        deps = ["requests", "urllib3", "internetarchive", "python-dotenv", "google-genai", "pydantic"]
        run([sys.executable, "-m", "pip", "install", *deps])

    # 3. Apt packages (aria2 untuk unduh model cepat, opsional jika belum ada)
    if os.name != "nt" and shutil.which("apt-get") and not shutil.which("aria2c"):
        try:
            run(["sudo", "apt-get", "install", "-y", "-qq", "aria2"])
        except Exception as e:
            print(f"  ·  Apt install error (ignored): {e}")

    print("\n✓  Setup descript selesai.\n")


def setup_guard():
    """Alias untuk setup_descript."""
    setup_descript()


# ── CLI ────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="VoT setup utility — install dependencies untuk recording, transkripsi, atau deskripsi.",
    )
    parser.add_argument(
        "mode",
        choices=["record", "transcript", "descript", "describe", "hybrid", "guard"],
        help="Mode: 'record', 'transcript', 'descript' (atau 'describe'), 'hybrid' (record+transcript), atau 'guard'",
    )

    args = parser.parse_args()

    if args.mode == "record":
        setup_record()
    elif args.mode == "transcript":
        setup_transcript()
    elif args.mode in ("descript", "describe", "guard"):
        setup_descript()
    elif args.mode == "hybrid":
        setup_record()
        setup_transcript()
if __name__ == "__main__":
    main()
