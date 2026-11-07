import re
import sys

if len(sys.argv) < 2:
    print("Usage: python3 clean.py <transcript.srt/txt>", file=sys.stderr)
    sys.exit(1)

raw = open(sys.argv[1], encoding="utf-8", errors="ignore").read()
pat = re.compile(r"(\d{2}):(\d{2}):(\d{2}),\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}\s*(.*?)(?=\s*\d+\s*\d{2}:\d{2}:\d{2},\d{3}\s*-->|\Z)", re.S)
groups, last = {}, []
for h, m, s, t in pat.findall(raw):
    sec = int(h)*3600 + int(m)*60 + int(s)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"(.)\1{4,}", r"\1", t)                      # titik/huruf berulang
    t = re.sub(r"\b(\w+)(?:[,\s]+\1\b){3,}", r"\1", t)      # kata berulang
    if not re.search(r"[A-Za-z0-9]", t) or t in last:       # kosong / duplikat
        continue
    last = (last + [t])[-3:]
    groups.setdefault(sec // 60, []).append((sec, t))
out = []
for k in sorted(groups):
    sec = groups[k][0][0]
    out.append(f"[{sec//3600:02d}:{sec%3600//60:02d}:{sec%60:02d}] " + " ".join(t for _, t in groups[k]))
txt = "\n".join(out)
print(txt)
print(f"~{len(txt)//3} token (perkiraan)", file=sys.stderr)
