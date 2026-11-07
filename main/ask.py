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

parsed_url = urlparse(server_url)
slots_url = f"{parsed_url.scheme}://{parsed_url.netloc}/slots"

# Thread pemantau proses prompt evaluation sebelum token pertama keluar
stop_monitor = threading.Event()

def monitor_slots():
    start_time = time.time()
    while not stop_monitor.wait(5):
        elapsed = int(time.time() - start_time)
        state_desc = "Sedang mengevaluasi prompt di CPU (RAM+Swap)..."
        try:
            req_s = urllib.request.Request(slots_url, headers={"User-Agent": "AskMonitor"})
            with urllib.request.urlopen(req_s, timeout=3) as r:
                slots = json.loads(r.read().decode())
                if slots and isinstance(slots, list):
                    s0 = slots[0]
                    state = s0.get("state", 0)
                    n_prompt = s0.get("n_prompt_tokens", 0)
                    n_decoded = s0.get("n_decoded", 0)
                    if state == 1:
                        state_desc = f"Mencerna prompt ({n_prompt} token)"
                    elif state == 2:
                        state_desc = f"Menghasilkan token ({n_decoded} token)"
        except Exception:
            pass
        print(f"[PROGRES {elapsed:>3}s] {state_desc}", flush=True)

monitor_thread = threading.Thread(target=monitor_slots, daemon=True)
monitor_thread.start()

print("[ INFO ] Mengirim request ke llama-server...", flush=True)
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
                                stop_monitor.set()
                                print("\n" + "=" * 65, flush=True)
                                print("[ LIVE STREAM ] Token mulai dihasilkan:", flush=True)
                                print("=" * 65, flush=True)
                            print(chunk, end="", flush=True)
                            collected.append(chunk)
                except Exception:
                    pass
finally:
    stop_monitor.set()

full_text = "".join(collected)
with open(out_file, "w", encoding="utf-8") as f:
    f.write(full_text)

print("\n\n" + "=" * 65, flush=True)
print(f"[ SUCCESS ] Selesai! Respon disimpan ke {out_file} ({len(full_text):,} karakter)", flush=True)
print("=" * 65, flush=True)
