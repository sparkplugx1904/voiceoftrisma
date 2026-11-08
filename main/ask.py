import json
import os
import re
import sys
import urllib.request
from urllib.parse import urlparse

prompt_file = sys.argv[1] if len(sys.argv) > 1 else "main/prompt.txt"
clean_file = sys.argv[2] if len(sys.argv) > 2 else "clean.txt"
out_file = sys.argv[3] if len(sys.argv) > 3 else "out.json"
server_url = sys.argv[4] if len(sys.argv) > 4 else "http://127.0.0.1:8080/v1/chat/completions"
think_file = "think.txt"

sys_prompt = open(prompt_file, encoding="utf-8").read()
tr = open(clean_file, encoding="utf-8").read()

prompt_tokens_est = len(sys_prompt) // 3
tr_tokens_est = len(tr) // 3
total_tokens_est = prompt_tokens_est + tr_tokens_est

print("=" * 65, flush=True)
print(f"[ INFO ] Ukuran prompt sistem    : ~{prompt_tokens_est:,} token", flush=True)
print(f"[ INFO ] Ukuran transkrip input : ~{tr_tokens_est:,} token", flush=True)
print(f"[ INFO ] Estimasi total input   : ~{total_tokens_est:,} token", flush=True)
print(f"[ INFO ] Server target          : {server_url}", flush=True)
print("=" * 65, flush=True)

# DeepSeek-R1 membutuhkan alokasi token yang lega untuk penalaran (<think>) + JSON utuh
body = {
    "messages": [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": "Transkrip:\n" + tr}
    ],
    "temperature": 0.6,
    "top_p": 0.95,
    "max_tokens": 8192,
    "stream": True
}

req = urllib.request.Request(
    server_url,
    json.dumps(body).encode("utf-8"),
    {"Content-Type": "application/json"}
)

print("[ INFO ] Mengirim request ke llama-server...", flush=True)
print("[ INFO ] Menunggu pemrosesan prompt di CPU... Aliran penalaran (<think>) dan token JSON akan langsung mengalir di bawah:\n", flush=True)

collected = []
first_token = False

try:
    with urllib.request.urlopen(req, timeout=None) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if not line or line.startswith(":"):
                continue
            if line == "data: [DONE]":
                break
            if line.startswith("data: "):
                payload = line[6:].strip()
                try:
                    data = json.loads(payload)
                    choices = data.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        chunk = delta.get("content", "")
                        if chunk:
                            if not first_token:
                                first_token = True
                                print("=" * 65, flush=True)
                                print("[ LIVE STREAM ] Aliran token aktif:", flush=True)
                                print("=" * 65, flush=True)
                            print(chunk, end="", flush=True)
                            collected.append(chunk)
                except Exception:
                    pass
except Exception as e:
    print(f"\n[ERROR] Koneksi atau inferensi gagal: {e}", flush=True)

full_text = "".join(collected).strip()

# ── 1. Ekstrak dan Simpan Jalur Penalaran (<think>...</think>) ─────────────────
think_text = ""
json_raw = full_text

think_match = re.search(r"<think>(.*?)(?:</think>|$)", full_text, re.DOTALL)
if think_match:
    think_text = think_match.group(1).strip()
    with open(think_file, "w", encoding="utf-8") as f:
        f.write(think_text)
    print(f"\n\n[ INFO ] Jalur penalaran (<think>) dipisahkan dan disimpan ke {think_file} ({len(think_text):,} karakter)", flush=True)

# Ambil bagian teks murni setelah </think>
if "</think>" in full_text:
    json_raw = full_text.split("</think>")[-1].strip()
elif "<think>" in full_text and "</think>" not in full_text:
    json_raw = full_text

# Bersihkan markdown fence ```json jika ada
json_raw = re.sub(r"^```(?:json)?\s*", "", json_raw, flags=re.MULTILINE)
json_raw = re.sub(r"\s*```$", "", json_raw, flags=re.MULTILINE).strip()

# ── 2. Parsing & Format JSON Berlekuk (indent=2) ──────────────────────────────
final_json_str = json_raw
parsed = None

# Cari blok {...} terluar
json_match = re.search(r"\{.*\}", json_raw, re.DOTALL)
candidate = json_match.group(0).strip() if json_match else json_raw

try:
    parsed = json.loads(candidate)
except Exception:
    try:
        decoder = json.JSONDecoder()
        text = candidate
        idx = 0
        objs = []
        while idx < len(text):
            while idx < len(text) and text[idx].isspace(): idx += 1
            if idx >= len(text): break
            obj, end = decoder.raw_decode(text, idx)
            objs.append(obj)
            idx = end
        if objs:
            combined = {}
            for o in objs:
                if isinstance(o, dict): combined.update(o)
            if "programs" in combined or "title" in combined:
                parsed = combined
    except Exception:
        pass

if parsed and isinstance(parsed, dict):
    # Safety guard: Tolak 'Sobat Trisma' jika model secara keliru menganggap panggilan pendengar sebagai penyiar
    for prog in parsed.get("programs", []):
        if isinstance(prog, dict) and prog.get("announcer", "").strip().lower() in ("sobat trisma", "sobat triisma"):
            prog["announcer"] = "Tidak teridentifikasi"
    final_json_str = json.dumps(parsed, indent=2, ensure_ascii=False)

with open(out_file, "w", encoding="utf-8") as f:
    f.write(final_json_str)

print("\n\n" + "=" * 65, flush=True)
print(f"[ SUCCESS ] Selesai! JSON valid berlekuk disimpan ke {out_file} ({len(final_json_str):,} karakter)", flush=True)
print("=" * 65, flush=True)
