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
    if not re.search(r"[A-Za-z0-9]", t):
        continue
    t_norm = re.sub(r"[^a-zA-Z0-9]", "", t.lower())
    if any(t_norm == re.sub(r"[^a-zA-Z0-9]", "", x.lower()) for x in last[-5:]):
        continue
    # Filter repetisi jingle/prompt Whisper di segmen hening/musik
    if re.search(r"voice of trisma.*(?:madyapadma|profil siswa)", t, re.I):
        if any(re.search(r"voice of trisma.*(?:madyapadma|profil siswa)", x, re.I) for x in last[-10:]):
            continue
    last = (last + [t])[-10:]
    groups.setdefault(sec // 60, []).append((sec, t))
out = []
for k in sorted(groups):
    sec = groups[k][0][0]
    out.append(f"[{sec//3600:02d}:{sec%3600//60:02d}:{sec%60:02d}] " + " ".join(t for _, t in groups[k]))
txt = "\n".join(out)
print(txt)
print(f"~{len(txt)//3} token (perkiraan)", file=sys.stderr)
