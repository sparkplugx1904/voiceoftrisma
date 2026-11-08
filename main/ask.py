import json
import os
import sys
import time
import threading
import urllib.request
from urllib.parse import urlparse

prompt_file = sys.argv[1] if len(sys.argv) > 1 else "main/prompt.txt"
clean_file = sys.argv[2] if len(sys.argv) > 2 else "clean.txt"
out_file = sys.argv[3] if len(sys.argv) > 3 else "out.json"
server_url = sys.argv[4] if len(sys.argv) > 4 else "http://127.0.0.1:8080/v1/chat/completions"

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

body = {
    "messages": [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": "Transkrip:\n" + tr}
    ],
    "temperature": 0.7,
    "top_p": 0.8,
    "top_k": 20,
    "presence_penalty": 1.5,
    "max_tokens": 3000,
    "stream": True
}

req = urllib.request.Request(
    server_url,
    json.dumps(body).encode("utf-8"),
    {"Content-Type": "application/json"}
)

print("[ INFO ] Mengirim request ke llama-server...", flush=True)
print("[ INFO ] Menunggu pemrosesan prompt di CPU... Token JSON akan langsung mengalir di bawah begitu inferensi dimulai:\n", flush=True)
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
                                print("\n" + "=" * 65, flush=True)
                                print("[ LIVE STREAM ] Token mulai dihasilkan:", flush=True)
                                print("=" * 65, flush=True)
                            print(chunk, end="", flush=True)
                            collected.append(chunk)
                except Exception:
                    pass
except Exception as e:
    print(f"\n[ERROR] Koneksi atau inferensi gagal: {e}", flush=True)

full_text = "".join(collected).strip()

# Parsing dan format rapi (indent=2) agar tidak terpotong satu baris di konsol log
final_json_str = full_text
try:
    decoder = json.JSONDecoder()
    text = full_text
    idx = 0
    objs = []
    while idx < len(text):
        while idx < len(text) and text[idx].isspace():
            idx += 1
        if idx >= len(text):
            break
        obj, end = decoder.raw_decode(text, idx)
        objs.append(obj)
        idx = end

    if objs:
        combined = {}
        for o in objs:
            if isinstance(o, dict):
                combined.update(o)
        if "programs" in combined or "title" in combined:
            final_json_str = json.dumps(combined, indent=2, ensure_ascii=False)
except Exception:
    pass

with open(out_file, "w", encoding="utf-8") as f:
    f.write(final_json_str)

print("\n\n" + "=" * 65, flush=True)
print(f"[ SUCCESS ] Selesai! Respon disimpan ke {out_file} ({len(final_json_str):,} karakter)", flush=True)
print("=" * 65, flush=True)
