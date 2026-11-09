#!/usr/bin/env python3
"""
Voice of Trisma — Descriptor Engine (Production).
Menghasilkan description.json dari transkrip siaran (.srt / .txt) menggunakan:
1. Model Utama: Local LLM Qwen3.6-35B-A3B (via llama-server / ask.py)
2. Fallback: Google Gemini API (jika server lokal tidak aktif dan key tersedia)
"""

import os
import re
import sys
import json
import time
import argparse
import subprocess
import urllib.request
from pathlib import Path
from typing import Optional, Dict, Any

_ROOT_DIR = Path(__file__).resolve().parent.parent
_MAIN_DIR = Path(__file__).resolve().parent

DEFAULT_SERVER_URL = os.environ.get("LLAMA_SERVER_URL") or "http://127.0.0.1:8080/v1/chat/completions"
DEFAULT_PROMPT_PATH = str(_MAIN_DIR / "prompt.txt")


def is_local_llama_server_ready(server_url: str = DEFAULT_SERVER_URL) -> bool:
    """Periksa apakah server inferensi lokal aktif dan siap menerima request."""
    try:
        health_url = server_url.replace("/v1/chat/completions", "/health")
        req = urllib.request.Request(health_url, headers={"User-Agent": "VoT-Descriptor/2.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = resp.read().decode("utf-8", errors="ignore").lower()
            return "ok" in data or "ready" in data or "status" in data or resp.status == 200
    except Exception:
        # Coba request OPTIONS / GET langsung ke server_url
        try:
            req = urllib.request.Request(server_url, method="OPTIONS")
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status < 500
        except Exception:
            return False


def generate_with_local_qwen(
    srt_path: str,
    output_path: str,
    server_url: str = DEFAULT_SERVER_URL,
    prompt_path: str = DEFAULT_PROMPT_PATH
) -> bool:
    """Hasilkan description.json menggunakan model Qwen lokal via clean.py dan ask.py."""
    print(f"[DESCRIPTOR] Menjalankan inferensi lokal Qwen via {server_url}...")
    srt_p = Path(srt_path)
    out_p = Path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)

    # 1. Bersihkan transkrip menggunakan main/clean.py
    clean_proc = subprocess.run(
        [sys.executable, str(_MAIN_DIR / "clean.py"), str(srt_p)],
        capture_output=True,
        text=True,
        check=True
    )
    clean_text = clean_proc.stdout.strip()
    if not clean_text:
        print("[DESCRIPTOR] [ERROR] Hasil pembersihan clean.py kosong!")
        return False

    temp_clean = _ROOT_DIR / "temp_clean_desc.txt"
    with open(temp_clean, "w", encoding="utf-8") as f:
        f.write(clean_text)

    try:
        # 2. Jalankan main/ask.py
        ask_cmd = [
            sys.executable,
            "-u",
            str(_MAIN_DIR / "ask.py"),
            prompt_path,
            str(temp_clean),
            str(out_p),
            server_url
        ]
        res = subprocess.run(ask_cmd, check=True)
        return out_p.exists() and out_p.stat().st_size > 10
    finally:
        if temp_clean.exists():
            try:
                temp_clean.unlink()
            except Exception:
                pass


def generate_with_gemini_fallback(
    srt_path: str,
    output_path: str,
    model_name: Optional[str] = None
) -> bool:
    """Fallback ke Gemini API jika server lokal tidak aktif dan GEMINI_API_KEY tersedia."""
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_key:
        print("[DESCRIPTOR] [WARN] GEMINI_API_KEY tidak tersedia untuk fallback.")
        return False

    print("[DESCRIPTOR] [FALLBACK] Menjalankan inferensi via Google Gemini API...")
    try:
        from google import genai
        client = genai.Client(api_key=gemini_key)

        with open(srt_path, "r", encoding="utf-8") as f:
            transcript = f.read()

        sys_prompt = open(DEFAULT_PROMPT_PATH, encoding="utf-8").read()

        candidate = model_name or "gemini-2.5-flash"
        response = client.models.generate_content(
            model=candidate,
            contents=[f"Transkrip:\n{transcript}"],
            config={"system_instruction": sys_prompt, "temperature": 0.6}
        )

        raw_text = response.text.strip()
        raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text, flags=re.MULTILINE)
        raw_text = re.sub(r"\s*```$", "", raw_text, flags=re.MULTILINE).strip()

        data = json.loads(raw_text)
        out_p = Path(output_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with open(out_p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        print(f"[DESCRIPTOR] [OK] Berhasil menghasilkan deskripsi via Gemini API: {out_p}")
        return True
    except Exception as e:
        print(f"[DESCRIPTOR] [ERROR] Fallback Gemini API gagal: {e}")
        return False


def generate_description(
    srt_file_path: str,
    output_file_path: str,
    server_url: str = DEFAULT_SERVER_URL,
    model_arg: Optional[str] = None
):
    """Entrypoint utama pembuatan description.json."""
    # 1. Coba local LLM (Qwen via llama-server) terlebih dahulu
    if is_local_llama_server_ready(server_url):
        ok = generate_with_local_qwen(srt_file_path, output_file_path, server_url=server_url)
        if ok:
            print(f"[DESCRIPTOR] [SUCCESS] description.json berhasil dibuat via model lokal Qwen: {output_file_path}")
            return

    # 2. Jika server lokal belum aktif, coba fallback ke Gemini API jika key ada
    if os.environ.get("GEMINI_API_KEY"):
        ok = generate_with_gemini_fallback(srt_file_path, output_file_path, model_name=model_arg)
        if ok:
            return

    print(f"[DESCRIPTOR] [FATAL] Server lokal Qwen ({server_url}) tidak aktif dan GEMINI_API_KEY tidak ditemukan!")
    print("  · Pastikan llama-server sedang berjalan di port 8080 atau sediakan GEMINI_API_KEY.")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Voice of Trisma Description Generator.")
    parser.add_argument("srt_path", help="Path ke berkas SRT atau transkrip teks.")
    parser.add_argument("-o", "--output", default="transcripts/description.json",
                        help="Path berkas output description.json (default: transcripts/description.json)")
    parser.add_argument("-s", "--server-url", default=DEFAULT_SERVER_URL,
                        help=f"URL server LLM OpenAI-compatible (default: {DEFAULT_SERVER_URL})")
    parser.add_argument("-m", "--model", default=None,
                        help="Model override (untuk fallback Gemini)")

    args, _ = parser.parse_known_args()
    generate_description(args.srt_path, args.output, server_url=args.server_url, model_arg=args.model)


if __name__ == "__main__":
    main()
