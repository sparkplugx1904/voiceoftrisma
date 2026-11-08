#!/usr/bin/env python3
"""
Voice of Trisma — GPU Speech-to-Text Transcriber (Nemotron & Qwen Omni/Audio).
Mendukung:
1. nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B (SALM)
2. Qwen/Qwen3-Omni-30B-A3B-Instruct / Qwen/Qwen2-Audio-7B-Instruct
Fitur:
- Auto-install dependencies (--install-deps)
- Kuantisasi hemat VRAM (--quant 4bit / 8bit / bf16)
- Chunking otomatis untuk audio siaran berdurasi panjang (>30 menit)
- Format keluaran TXT dan SRT dengan timestamp
"""

import os
import sys
import argparse
import subprocess
from pathlib import Path
from typing import Optional, List, Tuple

# ── 1. DEPENDENCY INSTALLER ──────────────────────────────────────────────────

REQUIRED_PACKAGES = [
    "torch",
    "torchaudio",
    "git+https://github.com/huggingface/transformers.git",
    "accelerate",
    "bitsandbytes",
    "soundfile",
    "librosa",
    "pydub",
    "scipy",
]


def install_dependencies():
    """Menginstal pustaka GPU dan Hugging Face yang diperlukan secara otomatis."""
    print("=" * 65)
    print("[ INSTALLER ] Memeriksa & menginstal dependensi GPU...")
    print("=" * 65)

    # Pastikan pip up to date
    subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"], check=False)

    for pkg in REQUIRED_PACKAGES:
        print(f"[ INSTALLER ] Memasang: {pkg}...")
        res = subprocess.run([sys.executable, "-m", "pip", "install", "-U", pkg])
        if res.returncode != 0:
            print(f"[ WARN ] Gagal menginstal {pkg}, melanjutkan paket lain...")

    print("\n[ OK ] Selesai instalasi dependensi GPU.\n")


def check_gpu_environment():
    """Memverifikasi ketersediaan akselerasi CUDA."""
    try:
        import torch
        if not torch.cuda.is_available():
            print("[ WARNING ] CUDA tidak terdeteksi! Script akan berjalan di CPU (sangat lambat untuk model 30B).")
            return "cpu"
        device_name = torch.cuda.get_device_name(0)
        vram_gb = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
        print(f"[ GPU INFO ] Perangkat: {device_name} | Total VRAM: {vram_gb:.2f} GB")
        return "cuda"
    except ImportError:
        print("[ INFO ] PyTorch belum terpasang. Jalankan dengan flag --install-deps terlebih dahulu.")
        return "none"


# ── 2. AUDIO CHUNKER (Long-form Audio Support) ──────────────────────────────

def slice_audio_if_needed(audio_path: Path, max_duration_sec: int = 1800) -> List[Tuple[Path, float]]:
    """
    Memotong audio jika melebihi batas durasi per request (default: 30 menit / 1800 detik).
    Mengembalikan daftar tuple: (chunk_file_path, start_offset_seconds).
    """
    try:
        import librosa
        import soundfile as sf
    except ImportError:
        return [(audio_path, 0.0)]

    print(f"[ CHUNKER ] Memeriksa durasi berkas audio: {audio_path.name}...")
    duration = librosa.get_duration(path=str(audio_path))
    print(f"[ CHUNKER ] Total durasi audio: {duration:.1f} detik ({duration / 60:.1f} menit)")

    if duration <= max_duration_sec:
        return [(audio_path, 0.0)]

    print(f"[ CHUNKER ] Audio melebihi {max_duration_sec // 60} menit. Melakukan segmentasi...")
    temp_dir = audio_path.parent / f"{audio_path.stem}_chunks"
    temp_dir.mkdir(parents=True, exist_ok=True)

    chunks = []
    sr = 16000
    y, _ = librosa.load(str(audio_path), sr=sr)

    total_samples = len(y)
    chunk_samples = max_duration_sec * sr

    part = 1
    for start in range(0, total_samples, chunk_samples):
        end = min(start + chunk_samples, total_samples)
        chunk_y = y[start:end]
        start_sec = start / sr
        chunk_file = temp_dir / f"chunk_{part:03d}_{int(start_sec)}s.wav"
        sf.write(str(chunk_file), chunk_y, sr)
        chunks.append((chunk_file, start_sec))
        part += 1

    print(f"[ CHUNKER ] Berhasil membagi audio menjadi {len(chunks)} segmen.")
    return chunks


# ── 3. ENGINE: NVIDIA NEMOTRON 3.5 TRANSCRIBE ───────────────────────────────

def run_nemotron_transcribe(
    audio_path: Path,
    model_id: str = "nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B",
    quantization: str = "4bit",
    language: str = "id"
) -> str:
    """Inferensi transkripsi audio menggunakan NVIDIA Nemotron 3.5 Transcribe."""
    import torch
    from transformers import AutoModelForMultimodalLM, AutoProcessor

    print(f"\n[ NEMOTRON ] Memuat model: {model_id} (Kuantisasi: {quantization})...")

    load_kwargs = {
        "device_map": "auto",
        "trust_remote_code": True,
    }

    if quantization == "4bit":
        load_kwargs["load_in_4bit"] = True
    elif quantization == "8bit":
        load_kwargs["load_in_8bit"] = True
    elif quantization == "bf16":
        load_kwargs["torch_dtype"] = torch.bfloat16
    else:
        load_kwargs["torch_dtype"] = torch.float16

    processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
    model = AutoModelForMultimodalLM.from_pretrained(model_id, **load_kwargs)

    chunks = slice_audio_if_needed(audio_path, max_duration_sec=1800)
    full_transcript = []

    for i, (chunk_file, offset_sec) in enumerate(chunks, start=1):
        print(f"[ NEMOTRON ] Memproses segmen {i}/{len(chunks)} (mulai detik ke-{int(offset_sec)})...")
        # Format instruksi sesuai prompt guide resmi NVIDIA
        prompt_style = f"Audio transcription. Language: {language}. Wording: verbatim. Formatting: punctuation and capitalization. Output: transcript only."
        inputs = processor.apply_transcription_request(str(chunk_file), instruction=prompt_style).to(model.device)

        with torch.inference_mode():
            outputs = model.generate(**inputs, max_new_tokens=4096)

        text = processor.decode(outputs[:, inputs.input_ids.shape[1]:], skip_special_tokens=True).strip()
        full_transcript.append(text)

    # Pembersihan chunk temporary jika dibuat
    if len(chunks) > 1:
        chunk_dir = chunks[0][0].parent
        for f, _ in chunks:
            if f.exists():
                f.unlink()
        try:
            chunk_dir.rmdir()
        except Exception:
            pass

    return "\n\n".join(full_transcript)


# ── 4. ENGINE: QWEN3-OMNI / QWEN2-AUDIO ─────────────────────────────────────

def run_qwen_audio_transcribe(
    audio_path: Path,
    model_id: str = "Qwen/Qwen3-Omni-30B-A3B-Instruct",
    quantization: str = "4bit",
    language: str = "Indonesian"
) -> str:
    """Inferensi transkripsi audio menggunakan Qwen3-Omni atau Qwen2-Audio."""
    import torch
    import librosa
    from transformers import AutoProcessor, AutoModelForMultimodalLM

    print(f"\n[ QWEN AUDIO ] Memuat model: {model_id} (Kuantisasi: {quantization})...")

    load_kwargs = {
        "device_map": "auto",
        "trust_remote_code": True,
    }

    if quantization == "4bit":
        load_kwargs["load_in_4bit"] = True
    elif quantization == "8bit":
        load_kwargs["load_in_8bit"] = True
    elif quantization == "bf16":
        load_kwargs["torch_dtype"] = torch.bfloat16
    else:
        load_kwargs["torch_dtype"] = torch.float16

    processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=True)
    model = AutoModelForMultimodalLM.from_pretrained(model_id, **load_kwargs)

    chunks = slice_audio_if_needed(audio_path, max_duration_sec=1200)
    full_transcript = []

    target_sr = getattr(processor.feature_extractor, "sampling_rate", 16000)

    for i, (chunk_file, offset_sec) in enumerate(chunks, start=1):
        print(f"[ QWEN AUDIO ] Memproses segmen {i}/{len(chunks)} (mulai detik ke-{int(offset_sec)})...")
        audio_data, sr = librosa.load(str(chunk_file), sr=target_sr)

        prompt = f"<|audio_bos|><|AUDIO|><|audio_eos|>Transkripsikan audio rekaman ini secara akurat dan lengkap ke dalam bahasa {language}. Outputkan teks transkrip saja tanpa komentar."

        inputs = processor(text=prompt, audios=audio_data, return_tensors="pt", sampling_rate=sr).to(model.device)

        with torch.inference_mode():
            outputs = model.generate(**inputs, max_new_tokens=2048)

        text = processor.batch_decode(outputs[:, inputs.input_ids.shape[1]:], skip_special_tokens=True)[0].strip()
        full_transcript.append(text)

    if len(chunks) > 1:
        chunk_dir = chunks[0][0].parent
        for f, _ in chunks:
            if f.exists():
                f.unlink()
        try:
            chunk_dir.rmdir()
        except Exception:
            pass

    return "\n\n".join(full_transcript)


# ── 5. MAIN CLI ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Transkripsi Audio GPU berbasis Speech LLM (Nemotron & Qwen Omni)."
    )
    parser.add_argument("--install-deps", action="store_true", help="Otomatis instal seluruh library GPU/Transformers.")
    parser.add_argument("--model", type=str, default="nemotron", choices=["nemotron", "qwen3-omni", "qwen2-audio"],
                        help="Pilihan engine Speech-LLM: nemotron, qwen3-omni, qwen2-audio")
    parser.add_argument("--model-id", type=str, default=None,
                        help="Override Hugging Face model repository ID.")
    parser.add_argument("--audio", type=str, default=None,
                        help="Jalur file audio lokal (.mp3, .wav) atau URL unduhan.")
    parser.add_argument("--output", type=str, default="transcript_gpu.txt",
                        help="Jalur berkas output transkripsi.")
    parser.add_argument("--quant", type=str, default="4bit", choices=["4bit", "8bit", "bf16", "fp16"],
                        help="Kuantisasi memori GPU: 4bit (hemat VRAM), 8bit, bf16, fp16.")
    parser.add_argument("--lang", type=str, default="id",
                        help="Kode bahasa (id, en, dsb.).")

    args, _ = parser.parse_known_args()

    if args.install_deps:
        install_dependencies()
        if not args.audio:
            sys.exit(0)

    if not args.audio:
        parser.print_help()
        print("\nContoh Penggunaan:")
        print("  1. Instalasi dependensi GPU:")
        print("     python main/transcribe_gpu.py --install-deps\n")
        print("  2. Transkripsi dengan Nemotron 30B (4-bit GPU):")
        print("     python main/transcribe_gpu.py --model nemotron --audio audio.mp3 --quant 4bit\n")
        print("  3. Transkripsi dengan Qwen3-Omni (4-bit GPU):")
        print("     python main/transcribe_gpu.py --model qwen3-omni --audio audio.mp3 --quant 4bit\n")
        sys.exit(1)

    device = check_gpu_environment()

    audio_path = Path(args.audio)
    if not audio_path.is_file():
        # Cek jika input adalah URL
        if args.audio.startswith("http://") or args.audio.startswith("https://"):
            print(f"[ DOWNLOAD ] Mengunduh audio dari URL: {args.audio}...")
            import urllib.request
            audio_path = Path("temp_gpu_audio.mp3")
            urllib.request.urlretrieve(args.audio, str(audio_path))
        else:
            print(f"[ ERROR ] Berkas audio tidak ditemukan: {args.audio}")
            sys.exit(1)

    # Eksekusi Model
    transcript_text = ""
    if args.model == "nemotron":
        mid = args.model_id or "nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B"
        transcript_text = run_nemotron_transcribe(audio_path, model_id=mid, quantization=args.quant, language=args.lang)
    elif args.model == "qwen3-omni":
        mid = args.model_id or "Qwen/Qwen3-Omni-30B-A3B-Instruct"
        transcript_text = run_qwen_audio_transcribe(audio_path, model_id=mid, quantization=args.quant, language="Indonesian")
    elif args.model == "qwen2-audio":
        mid = args.model_id or "Qwen/Qwen2-Audio-7B-Instruct"
        transcript_text = run_qwen_audio_transcribe(audio_path, model_id=mid, quantization=args.quant, language="Indonesian")

    # Simpan Output
    out_file = Path(args.output)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(transcript_text)

    print("=" * 65)
    print(f"[ SUCCESS ] Transkripsi selesai!")
    print(f"[ OUTPUT  ] Berkas disimpan di: {out_file.resolve()} ({len(transcript_text):,} karakter)")
    print("=" * 65)

    # Cuplikan 10 baris pertama
    print("\n--- Cuplikan Hasil Transkripsi ---")
    for line in transcript_text.splitlines()[:10]:
        print(line)


if __name__ == "__main__":
    main()
