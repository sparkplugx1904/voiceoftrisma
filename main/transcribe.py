#!/usr/bin/env python3

import sys
import os
import shutil
import subprocess
import re
import json
import traceback
from pathlib import Path
from typing import List, Tuple, Optional
import argparse
_MAIN_DIR = Path(__file__).resolve().parent
if str(_MAIN_DIR) not in sys.path:
    sys.path.insert(0, str(_MAIN_DIR))

try:
    from core.downloader import download_archive_file, get_archive_proxy_url
except ImportError:
    from main.core.downloader import download_archive_file, get_archive_proxy_url

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import numpy as np
except ImportError as e:
    print(f"[FATAL] FATAL: GAGAL MENGIMPOR PUSTAKA PENTING: {e}", file=sys.stderr)
    print("[FATAL] Pastikan Anda telah menjalankan 'pip install -r requirements/transcript' (minimal numpy)", file=sys.stderr)
    sys.exit(1)

# --- Sistem Logging Kustom Sederhana ---

def log_info(msg):
    """Mencatat pesan informasi."""
    print(f"[+] {msg}")

def log_success(msg):
    """Mencatat pesan sukses."""
    print(f"[OK] {msg}")

def log_warn(msg):
    """Mencatat pesan peringatan."""
    print(f"[!] {msg}")

def log_error(msg, exit_app=False):
    """Mencatat pesan error. Jika exit_app=True, hentikan skrip."""
    print(f"[X] ERROR: {msg}", file=sys.stderr)
    if exit_app:
        sys.exit(1)

# --- Konfigurasi ---
VALID_MODELS = ["tiny", "base", "small", "medium", "large-v1", "large-v2", "large-v3", "large-v3-turbo", "large-turbo-v3"]
MODEL_ALIASES = {
    "large-turbo-v3": "large-v3-turbo",
    "large_turbo_v3": "large-v3-turbo",
    "large-turbo": "large-v3-turbo",
    "large_v3_turbo": "large-v3-turbo",
    "turbo": "large-v3-turbo",
}
DEFAULT_MODEL_NAME = "large-v3-turbo"


def load_whisper_prompt() -> str:
    """Memuat initial prompt dari main/whisper.txt jika tersedia."""
    prompt_file = Path(__file__).resolve().parent / "whisper.txt"
    if prompt_file.exists():
        try:
            content = prompt_file.read_text(encoding="utf-8").strip()
            if content:
                lines = [line.strip() for line in content.splitlines() if line.strip() and not line.strip().startswith("#")]
                return " ".join(lines)
        except Exception as e:
            log_warn(f"Gagal membaca whisper.txt: {e}. Menggunakan fallback default prompt.")
    return "Voice of Trisma, Madyapadma, Sobat Trisma."


DEFAULT_PROMPT = load_whisper_prompt()
# --------------------

# --- Fungsi Inti ---

def is_real_binary(path: Path, min_size: int = 1000) -> bool:
    """Periksa apakah file ada, bukan file kosong, dan bukan Git LFS pointer text (anti Errno 8)."""
    if not path.is_file() or path.stat().st_size < min_size:
        return False
    try:
        with open(path, "rb") as f:
            header = f.read(16)
            if header.startswith(b"version https://"):
                return False
    except Exception:
        return False
    return True


def check_dependencies() -> Tuple[Path, Path]:
    """Memeriksa dependensi eksternal 'curl', 'ffmpeg', dan 'whisper-cli'."""
    if os.name != 'nt':
        os.system("chmod +x ./bin/linux-x64/* ./bin/* 2>/dev/null")
    log_info("Memeriksa dependensi...")
    dependencies_ok = True
    
    # 1. Cek curl
    if shutil.which("curl") is None:
        log_error("'curl' tidak ditemukan. Harap instal 'curl'.")
        dependencies_ok = False
        
    # 2. Cek ffmpeg
    # 2. Cek ffmpeg: prioritaskan ffmpeg bawaan runner (/usr/bin/ffmpeg) agar kebal pointer LFS
    ffmpeg_bin = None
    if shutil.which("ffmpeg"):
        ffmpeg_bin = Path(shutil.which("ffmpeg")).resolve()

    if not ffmpeg_bin:
        if os.name == 'nt':
            ffmpeg_candidates = [
                Path("bin/win-x64/ffmpeg.exe"),
                Path("bin/win64/ffmpeg.exe"),
                Path("bin/ffmpeg.exe"),
                Path("ffmpeg.exe"),
            ]
        else:
            ffmpeg_candidates = [
                Path("bin/linux-x64/ffmpeg"),
                Path("bin/ffmpeg"),
                Path("ffmpeg"),
            ]
        for c in ffmpeg_candidates:
            if is_real_binary(c):
                ffmpeg_bin = c.resolve()
                break
    if not ffmpeg_bin:
        log_error("'ffmpeg' tidak ditemukan. Pastikan ffmpeg terpasang.")
        dependencies_ok = False
        
    # 3. Cek whisper-cli
    whisper_cli_path = None
    if os.name == 'nt':
        whisper_candidates = [
            Path("bin/win-x64/whisper-cli.exe"),
            Path("bin/win64/whisper-cli.exe"),
            Path("bin/whisper-cli.exe"),
            Path("whisper-cli.exe"),
        ]
        for c in whisper_candidates:
            if c.exists():
                whisper_cli_path = c.resolve()
                break
        if not whisper_cli_path and shutil.which("whisper-cli"):
            whisper_cli_path = Path(shutil.which("whisper-cli")).resolve()
    else:
        whisper_candidates = [
            Path("bin/linux-x64/whisper-cli"),
            Path("bin/whisper-cli"),
            Path("whisper-cli"),
        ]
        for c in whisper_candidates:
            if c.exists():
                whisper_cli_path = c.resolve()
                break
        if not whisper_cli_path and shutil.which("whisper-cli"):
            whisper_cli_path = Path(shutil.which("whisper-cli")).resolve()
        
    if not whisper_cli_path or not whisper_cli_path.exists():
        log_error("whisper-cli tidak ditemukan. Pastikan binary whisper.cpp tersedia.")
        dependencies_ok = False
    
    if not dependencies_ok:
        log_error("Dependensi tidak lengkap. Keluar.", exit_app=True)
        
    log_success("Semua dependensi inti ditemukan.")
    return whisper_cli_path, ffmpeg_bin
    
def download_file(url: str, dest: Path) -> bool:
    """Mengunduh file menggunakan aria2c (jika ada) atau curl."""
    log_info(f"Mengunduh: {url} -> {dest}")
    os.makedirs(dest.parent, exist_ok=True)
    # Hapus file lama atau pointer Git LFS jika bukan binary valid agar aria2c tidak membuat file .1
    if dest.exists() and not is_real_binary(dest, min_size=1_000_000):
        try:
            dest.unlink()
        except Exception:
            pass

    # aria2c multi-thread (16 koneksi) hanya untuk Hugging Face CDN (model AI besar)
    # Audio dari Archive.org / Cloudflare Worker wajib pakai curl single-stream agar tidak terkena throttling/rate-limit!
    if "huggingface.co" in url and shutil.which("aria2c"):
        try:
            cmd = [
                "aria2c", "-x", "16", "-s", "16", "-k", "1M",
                "--allow-overwrite=true", "--auto-file-renaming=false",
                "--file-allocation=none", "--summary-interval=5",
                "--console-log-level=warn",
                "-d", str(dest.parent), "-o", dest.name, url
            ]
            subprocess.run(cmd, check=True)
            # Self-healing jika aria2c tetap membuat file .1
            dot_one = dest.parent / f"{dest.stem}.1{dest.suffix}"
            if dot_one.exists() and (not dest.exists() or not is_real_binary(dest, min_size=1_000_000)):
                if dest.exists():
                    dest.unlink()
                dot_one.replace(dest)

            if is_real_binary(dest, min_size=1_000_000):
                log_success(f"Unduhan selesai via aria2c: {dest}")
                return True
            log_warn("Hasil unduhan aria2c bukan binary valid, mencoba fallback ke curl...")
        except Exception as e:
            log_warn(f"aria2c gagal ({e}), mencoba fallback ke curl...")

    try:
        subprocess.run(
            [
                "curl", "-f", "-L", "-o", str(dest), "-m", "600",
                "--retry", "3", "--retry-delay", "2",
                "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                url
            ],
            check=True
        )
        print()
        log_success(f"Unduhan selesai: {dest}")
        return True
    except subprocess.CalledProcessError as e:
        print()
        log_error(f"Gagal mengunduh file (curl return code: {e.returncode}). URL: {url}", exit_app=False)
        if dest.exists():
            dest.unlink()
        return False
    except Exception as e:
        log_error(f"Terjadi error tak terduga saat mengunduh: {e}", exit_app=False)
        if dest.exists():
            dest.unlink()
        return False
def ensure_model_exists(model_name: str, custom_model_url: Optional[str]) -> Path:
    """Memastikan model GGML/GGUF ada di folder ./models/."""
    
    if custom_model_url:
        # --- FIX: Cek apakah ini path lokal yang sudah ada ---
        local_path = Path(custom_model_url)
        if local_path.exists() and local_path.is_file():
            log_success(f"Model kustom lokal ditemukan langsung: {local_path.resolve()}")
            return local_path
        # --- END FIX ---

        model_filename = Path(custom_model_url).name
        if not model_filename or '.' not in model_filename:
            log_error("URL model kustom tidak valid.", exit_app=True)
            
        model_path = Path(f"./models/{model_filename}")
        log_info(f"Memeriksa model kustom di cache: {model_path}")
        
        if is_real_binary(model_path, min_size=1_000_000):
            log_success(f"Model kustom ditemukan di cache: {model_path}")
            return model_path
        
        log_warn(f"Model kustom belum ada, mengunduh dari: {custom_model_url}")
        if not download_file(custom_model_url, model_path):
            log_error("Gagal mengunduh model kustom. Membatalkan.", exit_app=True)
        return model_path
            
    else:
        raw_model = model_name
        model_name = MODEL_ALIASES.get(model_name.lower(), model_name.lower())
        log_info(f"Memeriksa model standar: {model_name} (input: {raw_model})")
        if model_name not in VALID_MODELS and raw_model not in VALID_MODELS:
            log_error(f"Nama model standar tidak valid: '{raw_model}'. Pilihan: {', '.join(VALID_MODELS)}", exit_app=True)

        os.makedirs("models", exist_ok=True)
        model_path = Path(f"./models/ggml-{model_name}.bin")
        
        if is_real_binary(model_path, min_size=1_000_000):
            log_success(f"Model ditemukan: {model_path} ({model_path.stat().st_size / (1024 * 1024):.1f} MB)")
            return model_path
        if model_path.exists() and not is_real_binary(model_path, min_size=1_000_000):
            try:
                model_path.unlink()
            except Exception:
                pass

        log_warn(f"Model standar '{model_name}' belum ada, mengunduh...")
        url = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{model_name}.bin"
        if not download_file(url, model_path):
            log_error("Gagal mengunduh model standar. Membatalkan.", exit_app=True)
            
        return model_path

ARCHIVE_DOWNLOAD_PREFIX = "https://archive.org/download/"
DEFAULT_WORKER_PROXY = "https://voiceoftrisma.anandapradnyana68.workers.dev/archive/download"


def get_archive_proxy_url(url: str) -> Optional[str]:
    """Ubah URL archive.org/download/... menjadi URL reverse proxy Cloudflare Worker."""
    if url.startswith(ARCHIVE_DOWNLOAD_PREFIX):
        subpath = url[len(ARCHIVE_DOWNLOAD_PREFIX):]
        proxy_base = os.environ.get("CF_WORKER_PROXY_URL") or DEFAULT_WORKER_PROXY
        return f"{proxy_base.rstrip('/')}/{subpath}"
    return None


def download_audio(url: str, output_path: Path):
    """
    Wrapper untuk mengunduh file audio siaran.
    Mengutamakan Cloudflare Worker proxy untuk mencegah blokir/502/throttling
    Archive.org dengan verifikasi Content-Length dan fallback URL langsung.
    """
    log_info(f"Mengunduh audio siaran: {url}")
    if not download_archive_file(url, output_path, min_size=5_000_000, is_text=False):
        log_error("Gagal mengunduh audio siaran dari seluruh kandidat. Membatalkan.", exit_app=True)
    log_success(f"Audio berhasil diunduh ke {output_path} ({output_path.stat().st_size / (1024 * 1024):.1f} MB)")

def preprocess_audio(input_path: Path, ffmpeg_bin: Path) -> Path:
    """
    Membersihkan sinyal audio siaran radio sebelum ditranskripsi:
    - Memotong letupan nafas mic / plosive P-pop (highpass 80Hz)
    - Memotong desis elektrik tinggi / white noise seperti hujan (lowpass 7500Hz)
    - Meredam kebisingan latar studio (afftdn)
    - Menormalkan level vokal penyiar ke standar broadcast (loudnorm EBU R128)
    - Mengonversi format ke 16kHz Mono 16-bit WAV untuk whisper.cpp
    (Catatan: JANGAN gunakan silenceremove agar timeline/durasi audio WAV tetap identik 1:1 dengan audio asli)
    """
    clean_wav = input_path.with_name(f"{input_path.stem}_clean.wav")
    log_info(f"Pra-pemrosesan audio (filter desis & normalisasi loudness): {clean_wav.name}")
    
    filter_chain = (
        "highpass=f=80,"
        "lowpass=f=7500,"
        "afftdn=nf=-25,"
        "loudnorm=I=-16:TP=-1.5:LRA=11"
    )
    
    cmd = [
        str(ffmpeg_bin), "-y",
        "-i", str(input_path),
        "-vn",
        "-af", filter_chain,
        "-ar", "16000",
        "-ac", "1",
        "-c:a", "pcm_s16le",
        str(clean_wav)
    ]
    
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        log_success(f"Audio bersih siap untuk Whisper: {clean_wav}")
        return clean_wav
    except subprocess.CalledProcessError as e:
        log_warn(f"FFmpeg audio pre-processing gagal ({e}). Menggunakan audio asli.")
        return input_path
    except Exception as e:
        log_warn(f"Error saat pra-pemrosesan FFmpeg: {e}. Menggunakan audio asli.")
        return input_path

def transcribe_single_audio(audio_path: Path, model_path: Path, whisper_cli_path: Path, ffmpeg_bin: Path, prompt: Optional[str] = None):
    """Mentranskripsi seluruh file audio tunggal menggunakan whisper.cpp CLI."""
    if not prompt:
        prompt = DEFAULT_PROMPT or load_whisper_prompt()
    os.makedirs("transcripts", exist_ok=True)
    
    final_txt = Path("transcripts/transcript.txt")
    final_srt = Path("transcripts/transcript.srt")
    final_json = Path("transcripts/transcript.json")
    output_base_path_temp = audio_path.stem
    temp_txt_file = Path(output_base_path_temp).with_suffix(".txt")
    temp_srt_file = Path(output_base_path_temp).with_suffix(".srt")
    temp_json_file = Path(output_base_path_temp).with_suffix(".json")

    try:
        final_txt.write_text("", encoding="utf-8")
        final_srt.write_text("", encoding="utf-8")
        final_json.write_text("", encoding="utf-8")
        if temp_txt_file.exists(): temp_txt_file.unlink()
        if temp_srt_file.exists(): temp_srt_file.unlink()
        if temp_json_file.exists(): temp_json_file.unlink()
    except IOError as e:
        log_error(f"Gagal membersihkan/membuat file transkrip: {e}", exit_app=True)

    # 1. Pra-pemrosesan Audio via FFmpeg
    clean_audio_path = preprocess_audio(audio_path, ffmpeg_bin)
    
    log_info(f"Mentranskripsi: {clean_audio_path.name}")
    log_info(f"Prompt Whisper: '{prompt}'")
    n_threads = str(min(8, os.cpu_count() or 4))
        
    cmd = [
        str(whisper_cli_path),
        "-m", str(model_path),
        "-f", str(clean_audio_path),
        "--prompt", prompt,
        "--carry-initial-prompt", # Jaga panduan kata kunci stasiun sepanjang rekaman
        "-bo", "5",               # Best of 5 kandidat
        "-bs", "5",               # Beam search (mencegah kesalahan fonetik lokal)
        "-nf",                    # Matikan temperature fallback (cegah loop repetisi)
        "-mc", "32",              # Batasi context window agar tidak mencemari segmen
        "-t", n_threads,
        "--no-speech-thold", "0.65", # Tolak segmen hening/musik murni
        "--entropy-thold", "2.40",
        "--logprob-thold", "-1.00",
        "-sns",                   # Suppress non-speech tokens (lagu/noise)
        "-of", str(output_base_path_temp),
        "-otxt",
        "-osrt",
        "-oj",
        "-l", "id",
        "-pp"
    ]
    
    log_info(f"Menjalankan whisper-cli dengan parameter anti-looping & beam search...")
    
    try:
        subprocess.run(cmd, check=True, capture_output=False)
        print()
            
    except subprocess.CalledProcessError as e:
        print()
        log_error(f"whisper-cli GAGAL (return code: {e.returncode}). Proses dihentikan.", exit_app=True)
    except Exception as e:
        log_error(f"Error tak terduga saat menjalankan whisper-cli: {e}", exit_app=True)
    finally:
        # Bersihkan file audio hasil pembersihan sementara
        if clean_audio_path != audio_path and clean_audio_path.exists():
            try:
                clean_audio_path.unlink()
            except Exception:
                pass

    # Pindahkan TXT
    try:
        if temp_txt_file.exists():
            content = temp_txt_file.read_bytes().decode("utf-8", errors="replace").strip()
            final_txt.write_text(content, encoding="utf-8")
            temp_txt_file.unlink()
            log_success(f"TXT disimpan ke {final_txt}.")
        else:
            log_warn(f"File TXT output tidak ditemukan: {temp_txt_file}.")
    except Exception as e:
        log_error(f"Gagal memproses file TXT: {e}")

    # Pindahkan SRT
    try:
        if temp_srt_file.exists():
            content = temp_srt_file.read_bytes().decode("utf-8", errors="replace").strip()
            final_srt.write_text(content, encoding="utf-8")
            temp_srt_file.unlink()
            log_success(f"SRT disimpan ke {final_srt}.")
        else:
            log_warn(f"File SRT output tidak ditemukan: {temp_srt_file}.")
    except Exception as e:
        log_error(f"Gagal memproses file SRT: {e}")

    # Pindahkan JSON (pretty-print agar mudah dibaca)
    try:
        if temp_json_file.exists():
            raw = temp_json_file.read_bytes().decode("utf-8", errors="replace").strip()
            try:
                parsed = json.loads(raw)
                pretty = json.dumps(parsed, ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                log_warn("JSON dari whisper-cli tidak valid, disimpan apa adanya.")
                pretty = raw
            final_json.write_text(pretty, encoding="utf-8")
            temp_json_file.unlink()
            log_success(f"JSON disimpan ke {final_json}.")
        else:
            log_warn(f"File JSON output tidak ditemukan: {temp_json_file}.")
    except Exception as e:
        log_error(f"Gagal memproses file JSON: {e}")

    log_success("Transkripsi selesai.")

# -----------------------------------------------------
# FUNGSI MAIN
# -----------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Skrip transkripsi audio menggunakan whisper.cpp.",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("source", help="URL audio atau path file lokal.")
    parser_model_group = parser.add_mutually_exclusive_group(required=False)
    parser_model_group.add_argument(
        "model", 
        nargs='?', 
        default=DEFAULT_MODEL_NAME,
        help=f"Nama model standar ({', '.join(VALID_MODELS)}). Default: {DEFAULT_MODEL_NAME}"
    )
    parser_model_group.add_argument(
        "-cm", "--custom-model", 
        help="URL lengkap ke file model GGML/GGUF kustom (.bin/.gguf)."
    )
    parser.add_argument(
        "--prompt", 
        default=DEFAULT_PROMPT, 
        help=f"Prompt awal untuk memandu konteks dan kosakata. Default: '{DEFAULT_PROMPT}'"
    )
    
    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(1)
        
    args = parser.parse_args()

    original_audio_path = Path("original_audio_download")
    audio_path_to_process = None
    is_source_url = not os.path.exists(args.source)
    
    try:
        whisper_cli_path, ffmpeg_bin = check_dependencies()
        log_info(f"Source: {args.source}")
        
        # 1. Pastikan Model Tersedia
        model_path = ensure_model_exists(args.model, args.custom_model)
        
        # 2. Penentuan Path Audio Input
        if is_source_url:
            download_audio(args.source, original_audio_path)
            audio_path_to_process = original_audio_path
        else:
            log_info(f"Menggunakan file lokal: {args.source}")
            audio_path_to_process = Path(args.source)

        # 3. Transkripsi
        transcribe_single_audio(
            audio_path=audio_path_to_process,
            model_path=model_path,
            whisper_cli_path=whisper_cli_path,
            ffmpeg_bin=ffmpeg_bin,
            prompt=args.prompt
        )
        
    except Exception as e:
        log_error(f"Terjadi error fatal yang tidak terduga: {e}", exit_app=False)
        traceback.print_exc()
        sys.exit(1)
        
    finally:
        # 4. Pembersihan
        if is_source_url and original_audio_path.exists():
            try:
                original_audio_path.unlink()
                log_info(f"Berhasil menghapus: {original_audio_path}")
            except Exception as e:
                log_warn(f"Gagal menghapus {original_audio_path}: {e}")
        
        log_success("====== PROSES SELESAI ======")
        log_info("Output akhir ada di folder ./transcripts/ (transcript.txt, transcript.srt & transcript.json)")

# -----------------------------------------------------
# BLOK EKSEKUSI UTAMA
# -----------------------------------------------------
if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[FATAL] ERROR GLOBAL TIDAK TERDUGA: {e}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)