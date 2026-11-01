"""
VoT Core Timing — Polling Tunggu Siaran, Deteksi Cut-off, & Helper Penamaan File.
"""

import os
import re
import time
import requests
from typing import Optional

from core.logger import log, now_wita, get_headers, record_stats_entry, CONSOLE
import core.proxy as proxy_module

# =============================================
# Helper Penamaan File Rekaman
# =============================================
def make_base_no_ext(date_str: str, suffix: str = "") -> str:
    """Bangun path dasar file rekaman tanpa ekstensi."""
    suffix_part = f"-{suffix}" if suffix else ""
    return f"recordings/VOT-Denpasar_{date_str}{suffix_part}"

def get_next_chunk_filename(base_no_ext: str, ext: str) -> str:
    """Tentukan nama chunk berikutnya (_1, _2, dst.) atau file tunggal."""
    dirpath = os.path.dirname(base_no_ext) or "."
    base_name = os.path.basename(base_no_ext)
    pattern = re.compile(r"^" + re.escape(base_name) + r"(?:_(\d+))?\." + re.escape(ext) + r"$")

    max_index = -1
    found_any = False

    if os.path.exists(dirpath):
        for f in os.listdir(dirpath):
            m = pattern.match(f)
            if m:
                found_any = True
                if m.group(1):
                    try:
                        idx = int(m.group(1))
                        if idx > max_index:
                            max_index = idx
                    except Exception:
                        pass
                else:
                    if max_index < 0:
                        max_index = 0

    if not found_any:
        return f"{base_no_ext}.{ext}"

    next_idx = max_index + 1 if max_index >= 0 else 1
    return f"{base_no_ext}_{next_idx}.{ext}"

def is_cutoff_reached(no_timer: bool = False) -> bool:
    """Pengecekan apakah siaran sudah melewati batas waktu 18:30 WITA."""
    if no_timer:
        return False
    now = now_wita()
    return now.hour >= 18 and now.minute >= 30

# =============================================
# WAIT FOR STREAM
# =============================================
def wait_for_stream(url: str, args=None):
    """
    Strategi polling bertahap berbasis offset menit sejak jam :00.
    Tahap 1:  0– 2 menit → setiap   3 detik
    Tahap 2:  2– 3 menit → setiap   5 detik
    Tahap 3:  3– 5 menit → setiap  10 detik
    Tahap 4:  5–10 menit → setiap  15 detik
    Tahap 5: 10–30 menit → setiap  30 detik
    Tahap 6: 30–50 menit → setiap  60 detik
    Tahap 7: 50–60 menit → setiap  90 detik
    """
    STAGES = [
        (   0,  120,   3),
        ( 120,  180,   5),
        ( 180,  300,  10),
        ( 300,  600,  15),
        ( 600, 1800,  30),
        (1800, 3000,  60),
        (3000, 3600,  90),
    ]

    def get_interval(elapsed_seconds):
        for start, end, interval in STAGES:
            if start <= elapsed_seconds < end:
                return interval
        return 90

    def seconds_since_last_hour():
        now = now_wita()
        return now.minute * 60 + now.second

    stage_labels = {
        3: "0–2 mnt", 5: "2–3 mnt", 10: "3–5 mnt",
        15: "5–10 mnt", 30: "10–30 mnt", 60: "30–50 mnt", 90: "50–60 mnt",
    }
    last_interval = None
    last_err = None
    last_seen_hour = now_wita().hour
    ping_count = 0

    target_stats = "https://i.klikhost.com:8502/stats?json=1"
    log(f"[WAIT] Menunggu siaran — request ke stats: {target_stats}")

    random_proxy_mode = getattr(args, "random_proxy", None) if args else None
    if random_proxy_mode and proxy_module.PROXY_POOL:
        proxy_module.start_proxy_warmer()

    while True:
        if CONSOLE and CONSOLE.skip_stage.is_set():
            CONSOLE.skip_stage.clear()
            log("[SKIP] Wait stage dilewati oleh user.")
            proxy_module.stop_proxy_warmer()
            return

        current_hour = now_wita().hour
        elapsed = seconds_since_last_hour()

        if current_hour != last_seen_hour:
            log(f"[WAIT] Reset polling — jam :{current_hour:02d}:00 (kembali ke Tahap 1, interval 3s)")
            last_interval = None
            last_seen_hour = current_hour

        interval = get_interval(elapsed)
        mm, ss = divmod(elapsed, 60)

        if interval != last_interval:
            log(f"[WAIT] Tahap berubah → +{mm}m {ss:02d}s — interval ping: {interval}s ({stage_labels.get(interval, '?')})")
            last_interval = interval

        ping_count += 1
        ping_time = now_wita()
        t_start = time.monotonic()
        stat_entry = {
            "time"       : ping_time.strftime("%Y-%m-%d %H:%M:%S"),
            "ping_no"    : ping_count,
            "proxy"      : proxy_module.SELECTED_PROXY,
            "status_code": None,
            "stream_on_air": None,
            "response_ms": None,
            "error"      : None,
            "stats_body" : None,
        }

        try:
            use_proxy_for_stats = False
            if args:
                if getattr(args, "proxy", None):
                    use_proxy_for_stats = True
                if getattr(args, "random_proxy", None) in ["stats", "all"]:
                    use_proxy_for_stats = True

            stats_proxy = proxy_module.get_proxies_dict(proxy_module.SELECTED_PROXY) if use_proxy_for_stats else {"http": None, "https": None}

            response = requests.get(
                target_stats,
                timeout=5,
                verify=False,
                headers=get_headers(),
                proxies=stats_proxy
            )
            stat_entry["response_ms"] = round((time.monotonic() - t_start) * 1000)
            stat_entry["status_code"] = response.status_code

            if response.status_code == 200:
                try:
                    stat_entry["stats_body"] = response.json()
                except Exception:
                    stat_entry["stats_body"] = response.text[:500]

                res_clean = response.text.replace(" ", "")
                if "streamstatus" not in res_clean:
                    stat_entry["stream_on_air"] = None
                    stat_entry["error"] = "invalid_content"
                    if use_proxy_for_stats:
                        log(f"[PROXY-FAIL] #{ping_count} | Proxy {proxy_module.SELECTED_PROXY} merespons tanpa 'streamstatus'. Ganti proxy...")
                        if random_proxy_mode:
                            proxy_module.next_proxy_from_pool()
                    else:
                        log(f"[ERROR] #{ping_count} | Endpoint langsung merespons tanpa 'streamstatus'.")
                    last_err = None

                elif '"streamstatus":1' in res_clean:
                    stat_entry["stream_on_air"] = True
                    record_stats_entry(stat_entry)
                    log(f"[OK] Stream ON-AIR — memulai perekaman...")
                    proxy_module.stop_proxy_warmer()
                    with proxy_module.PROXY_WARM_LOCK:
                        if proxy_module.PROXY_WARM_POOL and random_proxy_mode:
                            proxy_module.SELECTED_PROXY = proxy_module.PROXY_WARM_POOL.popleft()
                            log(f"[POOL] Proxy dari warm pool disiapkan untuk recording: {proxy_module.SELECTED_PROXY} (sisa: {len(proxy_module.PROXY_WARM_POOL)})")
                    return
                else:
                    stat_entry["stream_on_air"] = False
                    if last_err != "offline":
                        log(f"[OFFLINE] Server merespons tapi siaran OFF-AIR — menunggu...")
                        last_err = "offline"
                    else:
                        log(f"[PING] #{ping_count} | Status: OFF-AIR | Interval: {interval}s")
            else:
                stat_entry["stream_on_air"] = False
                if use_proxy_for_stats and random_proxy_mode:
                    log(f"[PROXY-FAIL] #{ping_count} | Proxy {proxy_module.SELECTED_PROXY} HTTP {response.status_code} — ganti proxy...")
                    proxy_module.next_proxy_from_pool()
                    last_err = None
                else:
                    if last_err != f"http_{response.status_code}":
                        log(f"[OFFLINE] Server merespons HTTP {response.status_code} — menunggu...")
                        last_err = f"http_{response.status_code}"
                    else:
                        log(f"[PING] #{ping_count} | Status: HTTP {response.status_code} | Interval: {interval}s")

        except requests.exceptions.ProxyError as e:
            stat_entry["error"] = f"ProxyError:{type(e).__name__}"
            stat_entry["response_ms"] = round((time.monotonic() - t_start) * 1000)
            log(f"[PROXY-FAIL] #{ping_count} | Proxy {proxy_module.SELECTED_PROXY} tidak dapat digunakan: {type(e).__name__}")
            if random_proxy_mode:
                proxy_module.next_proxy_parallel_from_pool()
                record_stats_entry(stat_entry)
                time.sleep(1)
                continue
            else:
                log("[WARN] Proxy gagal, fallback ke koneksi langsung.")
                proxy_module.SELECTED_PROXY = None
            last_err = None

        except requests.exceptions.RequestException as e:
            stat_entry["error"] = f"RequestException:{type(e).__name__}"
            stat_entry["response_ms"] = round((time.monotonic() - t_start) * 1000)
            if use_proxy_for_stats:
                log(f"[PROXY-FAIL] #{ping_count} | Gagal via proxy: {type(e).__name__}")
                if random_proxy_mode:
                    proxy_module.next_proxy_parallel_from_pool()
                    record_stats_entry(stat_entry)
                    time.sleep(1)
                    continue
            else:
                log(f"[ERROR] #{ping_count} | Gagal menjangkau server langsung: {type(e).__name__}")
            last_err = None

        record_stats_entry(stat_entry)
        now_dt = now_wita()
        secs_to_next_hour = 3600 - (now_dt.minute * 60 + now_dt.second)
        sleep_duration = min(interval, secs_to_next_hour + 1)
        time.sleep(sleep_duration)

# =============================================
# START-TIME HELPER
# =============================================
def parse_start_time(value: str):
    """Parse string waktu ke objek datetime WITA hari ini (hh, hh:mm, atau hh:mm:ss)."""
    parts = value.strip().split(":")
    if len(parts) == 1:
        h, m, s = int(parts[0]), 0, 0
    elif len(parts) == 2:
        h, m, s = int(parts[0]), int(parts[1]), 0
    elif len(parts) == 3:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    else:
        raise ValueError(f"Format waktu tidak valid: '{value}'. Gunakan hh, hh:mm, atau hh:mm:ss")

    if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= s <= 59):
        raise ValueError(f"Nilai waktu di luar rentang valid: '{value}'")

    now = now_wita()
    return now.replace(hour=h, minute=m, second=s, microsecond=0)

def wait_until_start_time(start_dt):
    """Tunggu hingga waktu start_dt (datetime WITA). Menampilkan countdown tiap 30 detik."""
    log(f"[WAIT] Menunggu hingga jam {start_dt.strftime('%H:%M:%S')} WITA sebelum mulai...")
    last_log = None

    while True:
        if CONSOLE and CONSOLE.skip_stage.is_set():
            CONSOLE.skip_stage.clear()
            log("[SKIP] Start-time wait dilewati oleh user.")
            return

        now = now_wita()
        remaining = (start_dt - now).total_seconds()
        if remaining <= 0:
            log(f"[START] Waktu mulai tercapai ({start_dt.strftime('%H:%M:%S')} WITA). Melanjutkan...")
            return

        bucket = int(remaining // 30)
        if bucket != last_log:
            last_log = bucket
            rem_int = int(remaining)
            hrs = rem_int // 3600
            mins = (rem_int % 3600) // 60
            secs = rem_int % 60
            if hrs:
                countdown_str = f"{hrs}j {mins}m {secs:02d}d"
            elif mins:
                countdown_str = f"{mins}m {secs:02d}d"
            else:
                countdown_str = f"{secs}d"
            log(f"[WAIT] Menunggu start-time — sisa: {countdown_str} hingga jam {start_dt.strftime('%H:%M:%S')} WITA")

        time.sleep(min(5, remaining))
