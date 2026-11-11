#!/usr/bin/env python3
"""
Test NeMo-Speech.cpp Transcribe — VoT Experimental Testing Runner
================================================================
Menguji coba inferensi ASR berbasis NeMo-Speech.cpp (NVIDIA Nemotron Speech)
untuk mentranskripsi rekaman audio siaran Voice of Trisma.

Fitur:
1. Mengakuisisi audio dari Archive.org terbaru, live radio stream, atau custom URL.
2. Melakukan pra-pemrosesan standar ASR (16 kHz, Mono, PCM 16-bit WAV via FFmpeg).
3. Mengeksekusi inferensi ASR menggunakan biner native `nemo-speech`.
4. Menghitung benchmark inferensi (kecepatan & Real-Time Factor / RTF).
5. Mengekspor ringkasan hasil transkrip ke console & $GITHUB_STEP_SUMMARY.
"""

import os
import sys
import time
import json
import shutil
import argparse
import subprocess
import urllib.request
from pathlib import Path
from typing import Optional, Tuple, Dict, Any

# Tambahkan path root & main ke sys.path
_MAIN_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _MAIN_DIR.parent
for p in (str(_MAIN_DIR), str(_ROOT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    from core.logger import log
except ImportError:
    try:
        from main.core.logger import log
    except ImportError:
        def log(msg: str):
            print(f"[NEMO-TEST] {msg}", flush=True)

ARCHIVE_SEARCH_URL = (
    "https://archive.org/advancedsearch.php?"
    "q=identifier:vot-denpasar-*&fl[]=identifier,title,publicdate"
    "&sort[]=publicdate+desc&rows=5&output=json"
)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) VoiceOfTrisma-NeMoTester/1.0"


def find_nemo_binary(custom_path: Optional[str] = None) -> Optional[Path]:
    """Mencari path executable nemo-speech di environment sistem."""
    if custom_path:
        candidate = Path(custom_path).resolve()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate

    # 1. Cari via PATH
    which_bin = shutil.which("nemo-speech")
    if which_bin:
        return Path(which_bin)

    # 2. Cari di ~/.local/bin/ (lokasi instalasi default install.sh)
    home_local = Path.home() / ".local" / "bin" / "nemo-speech"
    if home_local.is_file() and os.access(home_local, os.X_OK):
        return home_local

    # 3. Cari di bin/linux-x64/ atau bin/win-x64/
    for sub in ["bin/linux-x64/nemo-speech", "bin/win-x64/nemo-speech.exe", "./nemo-speech"]:
        p = _ROOT_DIR / sub
        if p.is_file():
            return p

    return None


def fetch_recent_archive_audio() -> Tuple[str, str, str]:
    """
    Mengambil identifier, nama file, dan direct download URL
    dari siaran radio VOT terbaru di Archive.org.
    """
    log("Mencari item siaran radio VOT terbaru di Archive.org...")
    req = urllib.request.Request(ARCHIVE_SEARCH_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    docs = data.get("response", {}).get("docs", [])
    if not docs:
        raise RuntimeError("Tidak ditemukan item vot-denpasar-* di Archive.org")

    # Ambil item teratas
    item = docs[0]
    identifier = item.get("identifier")
    title = item.get("title", "")

    # Identifikasi nama file mp3
    filename = title if title.lower().endswith(".mp3") else f"{title}.mp3"
    download_url = f"https://archive.org/download/{identifier}/{filename}"
    log(f"Item terbaru terdeteksi: {identifier} | File: {filename}")
    log(f"Download URL: {download_url}")
    return identifier, filename, download_url


def acquire_audio(
    source: str,
    custom_url: str,
    duration: str,
    start_offset: str,
    output_wav: Path
) -> Tuple[float, Dict[str, Any]]:
    """
    Mengunduh/memotong dan mengonversi audio ke format WAV 16kHz Mono 16-bit PCM
    menggunakan FFmpeg untuk masukan NeMo-Speech.
    """
    output_wav.parent.mkdir(parents=True, exist_ok=True)
    meta: Dict[str, Any] = {"source": source}

    if source == "synthetic_sample":
        dur = float(duration) if duration.isdigit() else 10.0
        log(f"Membuat audio sample sintetis ({dur} detik) untuk pengujian...")
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"sine=frequency=1000:duration={dur}",
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
            str(output_wav)
        ]
        meta["info"] = f"Synthetic sine wave ({dur}s)"
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return dur, meta

    # Tentukan input audio target
    if source == "archive_recent":
        identifier, filename, target_url = fetch_recent_archive_audio()
        meta["identifier"] = identifier
        meta["filename"] = filename
        meta["url"] = target_url
    elif source == "stream_live":
        stream_url = os.environ.get("STREAM_URL") or "http://i.klikhost.com:8502/stream"
        log(f"Menghubungkan ke live streaming radio: {stream_url}")
        target_url = stream_url
        meta["url"] = target_url
    elif source == "custom_url":
        if not custom_url:
            raise ValueError("URL audio wajib diisi bila memilih sumber audio 'custom_url'!")
        target_url = custom_url
        meta["url"] = target_url
    else:
        raise ValueError(f"Sumber audio tidak dikenal: {source}")

    log(f"Memproses potongan audio siaran via FFmpeg -> {output_wav.name}...")
    cmd = ["ffmpeg", "-y"]

    # Offset awal hanya berlaku untuk rekaman file arsip/URL, bukan live stream
    if source in ("archive_recent", "custom_url") and start_offset and start_offset != "0":
        cmd.extend(["-ss", str(start_offset)])

    cmd.extend(["-i", target_url])

    # Batasi durasi jika ditentukan
    if duration and duration.lower() != "full" and duration.isdigit():
        cmd.extend(["-t", str(duration)])

    # Standarisasi parameter ASR: 16 kHz Mono PCM 16-bit
    cmd.extend(["-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(output_wav)])

    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0 or not output_wav.exists() or output_wav.stat().st_size < 1000:
        log(f"FFmpeg error: {res.stderr}")
        raise RuntimeError(f"Gagal memotong/mengonversi audio siaran menjadi WAV: {res.stderr[:200]}")

    # Hitung durasi aktual file WAV
    file_bytes = output_wav.stat().st_size
    # Tiap detik: 16000 sample * 1 channel * 2 byte = 32000 bytes (header WAV ~44 bytes)
    actual_duration = max(0.1, (file_bytes - 44) / 32000.0)
    meta["duration_seconds"] = round(actual_duration, 2)
    meta["file_size_bytes"] = file_bytes
    log(f"Audio masukan siap: {output_wav} ({actual_duration:.2f} detik, {file_bytes / 1024:.1f} KB)")
    return actual_duration, meta


def run_transcription(
    nemo_bin: Path,
    audio_wav: Path,
    model: str,
    stream: bool,
    output_dir: Path
) -> Dict[str, Any]:
    """
    Mengeksekusi perintah CLI nemo-speech transcribe dan mendokumentasikan hasilnya.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    txt_out = output_dir / "nemotron_transcript.txt"
    srt_out = output_dir / "nemotron_transcript.srt"
    json_out = output_dir / "nemotron_transcript.json"

    base_cmd = [str(nemo_bin), "transcribe", str(audio_wav)]
    if model and model.lower() != "auto":
        base_cmd.extend(["--model", model])
    if stream:
        base_cmd.append("--stream")

    log(f"Menjalankan perintah ASR: {' '.join(base_cmd)}")
    start_time = time.perf_counter()

    # Eksekusi transkripsi utama
    res = subprocess.run(base_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    elapsed = time.perf_counter() - start_time

    if res.returncode != 0:
        log(f"Error dari nemo-speech transcribe: {res.stderr}")
        raise RuntimeError(f"nemo-speech gagal mengeksekusi transkripsi (exit code {res.returncode}): {res.stderr[:300]}")

    transcript_text = res.stdout.strip()
    txt_out.write_text(transcript_text, encoding="utf-8")
    log(f"Transkrip teks berhasil disimpan ke {txt_out} ({len(transcript_text)} karakter)")

    # Upayakan ekspor format SRT jika didukung
    try:
        srt_cmd = base_cmd + ["--format", "srt", "-o", str(srt_out)]
        subprocess.run(srt_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    except Exception:
        pass

    # Upayakan ekspor format JSON jika didukung
    try:
        json_cmd = base_cmd + ["--json"]
        j_res = subprocess.run(json_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=60)
        if j_res.returncode == 0 and j_res.stdout.strip():
            json_out.write_text(j_res.stdout.strip(), encoding="utf-8")
    except Exception:
        pass

    return {
        "success": True,
        "elapsed_seconds": round(elapsed, 2),
        "transcript_text": transcript_text,
        "txt_path": str(txt_out),
        "srt_path": str(srt_out) if srt_out.exists() else None,
        "json_path": str(json_out) if json_out.exists() else None,
    }


def export_step_summary(audio_meta: Dict[str, Any], trans_meta: Dict[str, Any], model_name: str):
    """Mengekspor laporan pengujian rapi ke console dan GitHub Step Summary."""
    duration_s = audio_meta.get("duration_seconds", 0)
    elapsed_s = trans_meta.get("elapsed_seconds", 0)
    rtf = round(elapsed_s / duration_s, 3) if duration_s > 0 else 0

    transcript_preview = trans_meta.get("transcript_text", "")
    if len(transcript_preview) > 1000:
        transcript_preview = transcript_preview[:1000] + "\n\n... (dipotong untuk preview, lihat artifact lengkap)"
    if not transcript_preview:
        transcript_preview = "*(Tidak ada teks yang terdeteksi / hening)*"

    markdown_summary = f"""### 🎙️ Hasil Pengujian NeMo-Speech.cpp (Nemotron ASR)

| Parameter | Nilai |
| :--- | :--- |
| **Model Nemotron** | `{model_name}` |
| **Sumber Audio** | `{audio_meta.get('source')}` |
| **Durasi Audio** | `{duration_s:.2f} detik` |
| **Waktu Pemrosesan CPU** | `{elapsed_s:.2f} detik` |
| **Real-Time Factor (RTF)** | `{rtf}x` *(semakin kecil semakin cepat)* |
| **Status Eksekusi** | `BERHASIL (SUCCESS)` |

#### 📝 Cuplikan Transkrip Hasil Pengujian:
```text
{transcript_preview}
```
"""
    print("\n" + "=" * 60)
    print("HASIL PENGUJIAN NEMO-SPEECH.CPP")
    print("=" * 60)
    print(markdown_summary)

    # Tulis ke $GITHUB_STEP_SUMMARY bila berjalan di runner CI
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        try:
            with open(summary_file, "a", encoding="utf-8") as f:
                f.write("\n" + markdown_summary + "\n")
            log("Ringkasan berhasil ditulis ke $GITHUB_STEP_SUMMARY")
        except Exception as e:
            log(f"Gagal menulis ke $GITHUB_STEP_SUMMARY: {e}")


def main():
    parser = argparse.ArgumentParser(description="Uji Coba Transkripsi Audio Siaran via NeMo-Speech.cpp")
    parser.add_argument("--source", choices=["archive_recent", "stream_live", "custom_url", "synthetic_sample"],
                        default="archive_recent", help="Sumber audio siaran untuk pengujian")
    parser.add_argument("--url", default="", help="URL audio siaran kustom jika --source custom_url")
    parser.add_argument("--duration", default="60", help="Durasi audio yang diuji (detik atau 'full')")
    parser.add_argument("--start-offset", default="00:03:00", help="Offset pemotongan audio (HH:MM:SS atau detik)")
    parser.add_argument("--model", default="nemotron-3.5", help="Nama model Nemotron (default: nemotron-3.5)")
    parser.add_argument("--stream", action="store_true", help="Gunakan mode streaming (--stream)")
    parser.add_argument("--output-dir", default="transcripts", help="Direktori hasil transkrip")
    parser.add_argument("--nemo-bin", default=None, help="Path khusus ke biner executable nemo-speech")
    parser.add_argument("--dry-run", action="store_true", help="Unduh dan potong audio tanpa eksekusi model AI")

    args = parser.parse_args()

    log("=== Memulai Pengujian NeMo-Speech.cpp Transcribe ===")
    log(f"Sumber: {args.source} | Durasi: {args.duration}s | Model: {args.model}")

    output_dir = Path(args.output_dir).resolve()
    temp_wav = output_dir / "test_audio_16k.wav"

    # 1. Akuisisi & Preprocessing Audio
    try:
        dur, audio_meta = acquire_audio(
            source=args.source,
            custom_url=args.url,
            duration=args.duration,
            start_offset=args.start_offset,
            output_wav=temp_wav
        )
    except Exception as e:
        log(f"Error akuisisi audio: {e}")
        sys.exit(1)

    if args.dry_run:
        log(f"[DRY-RUN] Audio berhasil disiapkan di {temp_wav}. Berhenti tanpa eksekusi model.")
        sys.exit(0)

    # 2. Lokasi biner nemo-speech
    nemo_bin = find_nemo_binary(args.nemo_bin)
    if not nemo_bin:
        log("Biner nemo-speech tidak ditemukan! Pastikan sudah terinstal via install.sh")
        sys.exit(1)
    log(f"Biner nemo-speech terdeteksi: {nemo_bin}")

    # 3. Eksekusi Transkripsi
    try:
        trans_res = run_transcription(
            nemo_bin=nemo_bin,
            audio_wav=temp_wav,
            model=args.model,
            stream=args.stream,
            output_dir=output_dir
        )
    except Exception as e:
        log(f"Error inferensi NeMo-Speech: {e}")
        sys.exit(1)

    # 4. Ringkasan & Step Summary
    export_step_summary(audio_meta, trans_res, args.model)
    log("=== Pengujian NeMo-Speech.cpp Selesai dengan Sukses ===")


if __name__ == "__main__":
    main()
