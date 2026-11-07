import json
import sys
import urllib.request

prompt_file = sys.argv[1] if len(sys.argv) > 1 else "main/prompt.txt"
clean_file = sys.argv[2] if len(sys.argv) > 2 else "clean.txt"
out_file = sys.argv[3] if len(sys.argv) > 3 else "out.json"
server_url = sys.argv[4] if len(sys.argv) > 4 else "http://127.0.0.1:8080/v1/chat/completions"

sys_prompt = open(prompt_file, encoding="utf-8").read()
tr = open(clean_file, encoding="utf-8").read()

body = {
    "messages": [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": "Transkrip:\n" + tr}
    ],
    "temperature": 0.7,
    "top_p": 0.8,
    "top_k": 20,
    "presence_penalty": 1.5,
    "max_tokens": 3000
}

req = urllib.request.Request(
    server_url,
    json.dumps(body).encode(),
    {"Content-Type": "application/json"}
)

r = json.load(urllib.request.urlopen(req, timeout=None))
print("Usage:", r.get("usage"))
content = r["choices"][0]["message"]["content"]
open(out_file, "w", encoding="utf-8").write(content)
print(f"[OK] Respon berhasil disimpan ke {out_file}")
