#!/usr/bin/env python3
"""
Voice of Trisma — Kaggle Dual T4 GPU Transcription Benchmark
=============================================================
Skrip mandiri untuk menguji transkripsi 5 rekaman siaran VOT menggunakan:
- nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B
- Qwen/Qwen3-Omni-30B-A3B-Instruct (atau Qwen2-Audio-7B-Instruct)

Didesain khusus untuk lingkungan Kaggle GPU (Dual Tesla T4 / T4 x 2):
1. Menginstal seluruh dependensi secara otomatis saat pertama kali dijalankan.
2. Membagi bobot model secara otomatis melintasi GPU 0 & GPU 1 via device_map="auto" (4-bit bitsandbytes).
3. Mengunduh 5 berkas audio siaran VOT via Cloudflare Worker proxy dengan verifikasi Content-Length.
4. Melakukan segmentasi audio 20 menit (anti-OOM dan mematuhi batas konteks 1 jam Nemotron).
5. Menyimpan hasil transkrip ke /kaggle/working/transcripts/ atau ./transcripts/.
"""

import os
import re
import sys
import time
import shutil
import urllib.request
import argparse
from pathlib import Path
from typing import List, Tuple, Optional

# ── 1. OTOMASI INSTALASI DEPENDENSI KAGGLE GPU ──────────────────────────────

REQUIRED_PIP_PACKAGES = [
    "git+https://github.com/huggingface/transformers.git",
    "accelerate>=0.26.0",
    "bitsandbytes>=0.42.0",
    "soundfile>=0.12.1",
    "librosa>=0.10.1",
    "pydub>=0.25.1",
    "scipy",
]


def ensure_dependencies():
    """Memeriksa dan menginstal pustaka yang dibutuhkan secara otomatis."""
    print("=" * 70)
    print("🚀 [1/5] MEMERIKSA & MEMASANG DEPENDENSI KAGGLE GPU")
    print("=" * 70)

    import importlib.util
    needed = []
    checks = {
        "transformers": "git+https://github.com/huggingface/transformers.git",
        "accelerate": "accelerate>=0.26.0",
        "bitsandbytes": "bitsandbytes>=0.42.0",
        "soundfile": "soundfile>=0.12.1",
        "librosa": "librosa>=0.10.1",
        "pydub": "pydub>=0.25.1",
        "scipy": "scipy",
    }
    for mod, pkg in checks.items():
        if importlib.util.find_spec(mod) is None:
            needed.append(pkg)

    if needed:
        print(f"[ INFO ] Menginstal paket yang belum terpasang: {', '.join(needed)}...")
        import subprocess
        for pkg in needed:
            print(f"  · Menginstal: {pkg}...")
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", pkg], check=False)
        print("[ OK ] Seluruh dependensi berhasil dipasang!\n")

        # Bersihkan cache sys.modules agar tidak tercampur modul lama dan baru
        for k in list(sys.modules.keys()):
            if k == "transformers" or k.startswith("transformers."):
                del sys.modules[k]
    else:
        print("[ OK ] Semua dependensi utama sudah terpasang.")


# ── 2. DETEKSI LINGKUNGAN DUAL GPU T4 ────────────────────────────────────────

def inspect_gpu_hardware():
    """Memeriksa status dan distribusi memori pada Dual Tesla T4."""
    print("\n" + "=" * 70)
    print("⚡ [2/5] DETEKSI HARDWARE GPU")
    print("=" * 70)

    import torch
    if not torch.cuda.is_available():
        print("[ WARN ] CUDA tidak terdeteksi! Skrip akan berjalan di CPU (sangat lambat untuk 30B).")
        return 0

    gpu_count = torch.cuda.device_count()
    print(f"[ OK ] Ditemukan {gpu_count} unit GPU CUDA:")
    for i in range(gpu_count):
        name = torch.cuda.get_device_name(i)
        vram = torch.cuda.get_device_properties(i).total_memory / (1024 ** 3)
        print(f"  · GPU [{i}]: {name} (VRAM: {vram:.2f} GB)")

    if gpu_count >= 2:
        print("[ INFO ] Dual T4 aktif! Model 30B 4-bit (~18 GB) akan dibagi rata (~9 GB per GPU).")
    return gpu_count


# ── 3. DAFTAR 5 BERKAS AUDIO SIARAN VOT ─────────────────────────────────────

SAMPLE_BROADCASTS = [
    {
        "item_id": "vot-denpasar-20261107-183001",
        "title": "Sabtu, 07 November 2026",
        "audio_name": "VOT-Denpasar_07-11-26-0.mp3",
    },
    {
        "item_id": "vot-denpasar-20261106-183004",
        "title": "Jumat, 06 November 2026",
        "audio_name": "VOT-Denpasar_06-11-26-0.mp3",
    },
    {
        "item_id": "vot-denpasar-20261105-183010",
        "title": "Kamis, 05 November 2026",
        "audio_name": "VOT-Denpasar_05-11-26-0.mp3",
    },
    {
        "item_id": "vot-denpasar-20261103-183002",
        "title": "Selasa, 03 November 2026",
        "audio_name": "VOT-Denpasar_03-11-26-0.mp3",
    },
    {
        "item_id": "vot-denpasar-20261102-183004",
        "title": "Senin, 02 November 2026",
        "audio_name": "VOT-Denpasar_02-11-26-0.mp3",
    },
]

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
DEFAULT_PROXY_BASE = "https://voiceoftrisma.anandapradnyana68.workers.dev/archive/download"


def download_broadcast_audio(item_id: str, audio_name: str, dest_dir: Path) -> Optional[Path]:
    """Mengunduh berkas audio via Worker proxy dengan verifikasi Content-Length."""
    dest_path = dest_dir / f"{item_id}_{audio_name}"
    if dest_path.exists() and dest_path.stat().st_size > 5_000_000:
        print(f"  [ CACHE ] Audio sudah ada: {dest_path.name} ({dest_path.stat().st_size / (1024**2):.1f} MB)")
        return dest_path

    sources = [
        f"{DEFAULT_PROXY_BASE}/{item_id}/{audio_name}",
        f"https://archive.org/download/{item_id}/{audio_name}",
    ]

    for attempt in range(1, 4):
        for url in sources:
            label = "Worker Proxy" if "workers.dev" in url else "Direct Archive.org"
            try:
                print(f"  · Mengunduh [{label}] (Attempt {attempt}/3): {audio_name}...")
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    if resp.status != 200:
                        continue
                    cl = resp.headers.get("Content-Length")
                    expected_len = int(cl) if cl and cl.isdigit() else None

                    tmp_path = dest_path.with_suffix(".tmp")
                    with open(tmp_path, "wb") as f:
                        shutil.copyfileobj(resp, f)

                    actual_size = tmp_path.stat().st_size
                    if expected_len and actual_size < expected_len:
                        print(f"  ⚠ Unduhan terpotong ({actual_size}/{expected_len} B). Retry...")
                        tmp_path.unlink()
                        continue

                    if actual_size < 5_000_000:
                        tmp_path.unlink()
                        continue

                    tmp_path.replace(dest_path)
                    print(f"  ✓ Berhasil diunduh: {dest_path.name} ({actual_size / (1024**2):.1f} MB)")
                    return dest_path
            except Exception as e:
                print(f"  ⚠ Gagal dari {label}: {e}")
                time.sleep(2)

    return None


# ── 4. AUDIO CHUNKER (SEGMENTASI 20 MENIT) ──────────────────────────────────

def split_audio_into_segments(audio_path: Path, max_min: int = 20) -> List[Tuple[Path, float, float]]:
    """
    Membagi siaran 2 jam menjadi potongan 20 menit agar mematuhi batas konteks
    1 jam Nemotron dan mencegah Out-of-Memory pada VRAM GPU.
    Mengembalikan daftar tuple: (chunk_path, start_sec, end_sec).
    """
    import librosa
    import soundfile as sf

    print(f"  · Menganalisis durasi audio: {audio_path.name}...")
    total_sec = librosa.get_duration(path=str(audio_path))
    print(f"  · Total durasi: {total_sec:.1f} detik ({total_sec / 60:.1f} menit)")

    chunk_sec = max_min * 60
    if total_sec <= chunk_sec:
        return [(audio_path, 0.0, total_sec)]

    chunk_dir = audio_path.parent / f"{audio_path.stem}_chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)

    chunks = []
    sr = 16000
    y, _ = librosa.load(str(audio_path), sr=sr)
    total_samples = len(y)
    step_samples = chunk_sec * sr

    idx = 1
    for start_sample in range(0, total_samples, step_samples):
        end_sample = min(start_sample + step_samples, total_samples)
        segment = y[start_sample:end_sample]
        s_sec = start_sample / sr
        e_sec = end_sample / sr

        chunk_file = chunk_dir / f"part_{idx:02d}_{int(s_sec)}s-{int(e_sec)}s.wav"
        if not chunk_file.exists() or chunk_file.stat().st_size == 0:
            sf.write(str(chunk_file), segment, sr)
        chunks.append((chunk_file, s_sec, e_sec))
        idx += 1

    print(f"  ✓ Audio dipotong menjadi {len(chunks)} bagian ({max_min} menit per segmen).")
    return chunks


# ── 5. ENGINE MODEL NEMOTRON 3.5 TRANSCRIBE (4-BIT DUAL T4) ─────────────────

def load_nemotron_model(model_id: str = "nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B"):
    """Memuat model Nemotron 30B terkuantisasi 4-bit terdistribusi di Dual GPU T4."""
    import torch
    try:
        import transformers.utils
        if not hasattr(transformers.utils, "is_torchcodec_usable"):
            transformers.utils.is_torchcodec_usable = lambda: False
    except Exception:
        pass
    from transformers import AutoModelForMultimodalLM, AutoProcessor
    print(f"\n[ MODEL ] Menginisialisasi Nemotron 3.5 Transcribe ({model_id})...")
    print("  · Mengaktifkan kuantisasi 4-bit bitsandbytes...")
    print("  · Mendistribusikan layer secara otomatis ke GPU 0 dan GPU 1...")

    processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
    model = AutoModelForMultimodalLM.from_pretrained(
        model_id,
        device_map="auto",
        load_in_4bit=True,
        trust_remote_code=True,
    )
    return processor, model


def transcribe_nemotron(processor, model, chunk_file: Path, language: str = "id") -> str:
    """Mentranskripsikan 1 segmen audio menggunakan Nemotron."""
    import torch
    instruction = (
        f"Audio transcription. Language: {language}. Wording: verbatim. "
        "Formatting: punctuation and capitalization. Output: transcript only."
    )
    inputs = processor.apply_transcription_request(str(chunk_file), instruction=instruction).to(model.device)

    with torch.inference_mode():
        outputs = model.generate(**inputs, max_new_tokens=4096)

    text = processor.decode(outputs[:, inputs.input_ids.shape[1]:], skip_special_tokens=True).strip()
    return text


# ── 6. ENGINE MODEL QWEN3-OMNI / QWEN2-AUDIO (4-BIT DUAL T4) ────────────────

def load_qwen_omni_model(model_id: str = "Qwen/Qwen3-Omni-30B-A3B-Instruct"):
    """Memuat model Qwen3-Omni 30B terkuantisasi 4-bit terdistribusi di Dual GPU T4."""
    import torch
    try:
        import transformers.utils
        if not hasattr(transformers.utils, "is_torchcodec_usable"):
            transformers.utils.is_torchcodec_usable = lambda: False
    except Exception:
        pass
    from transformers import AutoModelForMultimodalLM, AutoProcessor
    print(f"\n[ MODEL ] Menginisialisasi Qwen3-Omni ({model_id})...")
    print("  · Mengaktifkan kuantisasi 4-bit bitsandbytes...")
    print("  · Mendistribusikan layer secara otomatis ke GPU 0 dan GPU 1...")

    processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
    model = AutoModelForMultimodalLM.from_pretrained(
        model_id,
        device_map="auto",
        load_in_4bit=True,
        trust_remote_code=True,
    )
    return processor, model


def transcribe_qwen_omni(processor, model, chunk_file: Path, language: str = "Indonesian") -> str:
    """Mentranskripsikan 1 segmen audio menggunakan Qwen3-Omni."""
    import torch
    import librosa

    target_sr = getattr(processor.feature_extractor, "sampling_rate", 16000)
    audio_data, sr = librosa.load(str(chunk_file), sr=target_sr)

    prompt = (
        f"<|audio_bos|><|AUDIO|><|audio_eos|>Transkripsikan rekaman percakapan radio ini secara utuh, "
        f"akurat, dan lengkap ke dalam bahasa {language}. Hanya tampilkan teks hasil transkripsi."
    )
    inputs = processor(text=prompt, audios=audio_data, return_tensors="pt", sampling_rate=sr).to(model.device)

    with torch.inference_mode():
        outputs = model.generate(**inputs, max_new_tokens=2048)

    text = processor.batch_decode(outputs[:, inputs.input_ids.shape[1]:], skip_special_tokens=True)[0].strip()
    return text


# ── 7. FORMATTER SUBTITLE SRT ────────────────────────────────────────────────

def format_timestamp_srt(seconds: float) -> str:
    """Konversi detik ke format SRT: HH:MM:SS,mmm"""
    ms = int((seconds % 1) * 1000)
    total_sec = int(seconds)
    s = total_sec % 60
    m = (total_sec // 60) % 60
    h = total_sec // 3600
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ── 8. PIPELINE UTAMA PENGUJIAN 5 SIARAN ─────────────────────────────────────

def run_benchmark(engine: str = "nemotron", max_chunk_minutes: int = 20):
    """Menjalankan pengujian transkripsi 5 siaran di Kaggle GPU."""
    ensure_dependencies()
    inspect_gpu_hardware()

    # Tentukan direktori kerja (Kaggle working dir atau lokal)
    base_dir = Path("/kaggle/working") if Path("/kaggle/working").exists() else Path(".")
    audio_dir = base_dir / "audio_downloads"
    output_dir = base_dir / "transcripts"
    audio_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Muat Model ke Dual GPU T4
    t_start_load = time.time()
    if engine == "nemotron":
        processor, model = load_nemotron_model("nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B")
        transcribe_func = lambda cf: transcribe_nemotron(processor, model, cf)
    else:
        processor, model = load_qwen_omni_model("Qwen/Qwen3-Omni-30B-A3B-Instruct")
        transcribe_func = lambda cf: transcribe_qwen_omni(processor, model, cf)
    print(f"[ OK ] Model selesai dimuat ke Dual GPU T4 dalam {time.time() - t_start_load:.1f} detik.")

    print("\n" + "=" * 70)
    print(f"🎙️ [3/5] MEMULAI TRANSKRIPSI 5 SIARAN VOT ({engine.upper()})")
    print("=" * 70)

    summary_stats = []

    for idx, bc in enumerate(SAMPLE_BROADCASTS, start=1):
        item_id = bc["item_id"]
        title = bc["title"]
        audio_name = bc["audio_name"]

        print(f"\n──────────────────────────────────────────────────────────────────────")
        print(f"▶ [{idx}/5] Siaran: {title} ({item_id})")
        print(f"──────────────────────────────────────────────────────────────────────")

        t0 = time.time()

        # 1. Unduh Berkas Audio
        audio_file = download_broadcast_audio(item_id, audio_name, audio_dir)
        if not audio_file:
            print(f"❌ Gagal mengunduh audio untuk {item_id}. Melewati...")
            continue

        # 2. Segmentasi Audio
        segments = split_audio_into_segments(audio_file, max_min=max_chunk_minutes)

        # 3. Transkripsi Tiap Segmen
        full_text_blocks = []
        srt_entries = []
        srt_counter = 1

        for seg_idx, (seg_file, start_s, end_s) in enumerate(segments, start=1):
            print(f"  · Mentranskripsi segmen {seg_idx}/{len(segments)} ({int(start_s)}s - {int(end_s)}s)...")
            t_seg = time.time()
            text = transcribe_func(seg_file)
            dur_seg = time.time() - t_seg
            print(f"    ✓ Selesai dalam {dur_seg:.1f} detik: \"{text[:70]}...\"")

            full_text_blocks.append(f"[{format_timestamp_srt(start_s)} --> {format_timestamp_srt(end_s)}]\n{text}")

            srt_entries.append(
                f"{srt_counter}\n"
                f"{format_timestamp_srt(start_s)} --> {format_timestamp_srt(end_s)}\n"
                f"{text}\n"
            )
            srt_counter += 1

        total_time = time.time() - t0

        # 4. Simpan Hasil Transkripsi
        txt_out = output_dir / f"{item_id}.txt"
        srt_out = output_dir / f"{item_id}.srt"

        with open(txt_out, "w", encoding="utf-8") as f:
            f.write("\n\n".join(full_text_blocks))

        with open(srt_out, "w", encoding="utf-8") as f:
            f.write("\n".join(srt_entries))

        print(f"  ✓ Output TXT: {txt_out.name} ({txt_out.stat().st_size} bytes)")
        print(f"  ✓ Output SRT: {srt_out.name} ({srt_out.stat().st_size} bytes)")
        print(f"  ⏱️ Total Waktu: {total_time / 60:.1f} menit ({total_time:.1f}s)")

        summary_stats.append({
            "item_id": item_id,
            "title": title,
            "segments": len(segments),
            "txt_size": txt_out.stat().st_size,
            "time_sec": total_time,
        })

    # ── Ringkasan Hasil Pengujian ──
    print("\n" + "=" * 70)
    print("📊 [4/5] RINGKASAN HASIL PENGUJIAN 5 SIARAN DI KAGGLE GPU")
    print("=" * 70)
    print(f"| {'Tanggal':<25} | {'Item ID':<28} | {'Segmen':<6} | {'Ukuran TXT':<10} | {'Waktu':<8} |")
    print(f"|{'-'*27}|{'-'*30}|{'-'*8}|{'-'*12}|{'-'*10}|")
    for s in summary_stats:
        print(f"| {s['title']:<25} | {s['item_id']:<28} | {s['segments']:<6} | {s['txt_size']:<10} | {s['time_sec']/60:4.1f}m   |")
    print("=" * 70)
    print(f"[ OK ] Semua berkas transkrip tersimpan di folder: {output_dir.resolve()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pengujian Transkripsi 5 Siaran di Kaggle Dual T4 GPU.")
    parser.add_argument("--engine", type=str, default="nemotron", choices=["nemotron", "qwen3-omni"],
                        help="Pilihan model: nemotron (NVIDIA Nemotron 3.5 30B) atau qwen3-omni (Qwen3-Omni 30B)")
    parser.add_argument("--chunk-minutes", type=int, default=20,
                        help="Durasi segmentasi audio dalam menit (default: 20 menit per potongan)")
    args, _ = parser.parse_known_args()

    run_benchmark(engine=args.engine, max_chunk_minutes=args.chunk_minutes)
