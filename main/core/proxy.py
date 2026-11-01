"""
VoT Core Proxy — Manajemen Sumber Proxy, Concurrent Finder, & Warm Pool.
"""

import os
import json
import time
import requests
import itertools
import threading
import concurrent.futures
from collections import deque
from typing import Optional, List, Tuple

from core.logger import log, get_headers

# =============================================
# State Proxy Global
# =============================================
SELECTED_PROXY: Optional[str] = None
PROXY_POOL: List[str] = []
PROXY_POOL_INDEX: int = 0
PROXY_SOURCES: List[str] = []

PROXY_WARM_POOL: deque = deque()
PROXY_WARM_LOCK = threading.Lock()
PROXY_WARM_TARGET = 16
_proxy_warmer_stop = threading.Event()

def load_proxy_sources() -> List[str]:
    """Muat daftar sumber URL proxy dari environment variable HTTP_PROXY (JSON array)."""
    raw = os.environ.get("HTTP_PROXY", "").strip()
    if not raw:
        log("[WARN] Environment variable HTTP_PROXY tidak ditemukan atau kosong. Proxy source list kosong.")
        return []
    try:
        sources = json.loads(raw)
        if not isinstance(sources, list):
            log("[WARN] HTTP_PROXY bukan JSON array yang valid. Proxy source list kosong.")
            return []
        sources = [s for s in sources if isinstance(s, str) and s.strip()]
        log(f"[ENV] HTTP_PROXY dimuat: {len(sources)} sumber proxy ditemukan.")
        return sources
    except json.JSONDecodeError as e:
        log(f"[WARN] Gagal parse HTTP_PROXY sebagai JSON: {e}. Proxy source list kosong.")
        return []

def get_proxies_dict(proxy_url: Optional[str]):
    """Format string proxy 'ip:port' menjadi dict untuk library requests."""
    if not proxy_url:
        return None
    if not proxy_url.startswith("http"):
        proxy_url = f"http://{proxy_url}"
    return {"http": proxy_url, "https": proxy_url}

def normalize_proxy(raw: str) -> Optional[str]:
    """Normalisasi format 'http://ip:port' atau 'ip:port' ke 'ip:port' yang valid."""
    p = raw.strip()
    for prefix in ("https://", "http://"):
        if p.lower().startswith(prefix):
            p = p[len(prefix):]
            break
    if ":" not in p:
        return None
    parts = p.rsplit(":", 1)
    if len(parts) != 2:
        return None
    host, port_str = parts
    if not port_str.isdigit():
        return None
    port = int(port_str)
    if not (1 <= port <= 65535) or not host:
        return None
    return f"{host}:{port}"

def find_working_proxy() -> Optional[str]:
    """
    Unduh seluruh sumber proxy secara concurrent, normalisasi,
    lalu cari proxy aktif pertama yang merespons 200 OK + 'streamstatus'.
    """
    global PROXY_POOL, PROXY_POOL_INDEX, PROXY_SOURCES, SELECTED_PROXY
    if not PROXY_SOURCES:
        PROXY_SOURCES = load_proxy_sources()

    if not PROXY_SOURCES:
        log("[PROXY-FAIL] PROXY_SOURCES kosong. Pastikan HTTP_PROXY sudah diset.")
        return None

    log("[PROXY-SEARCH] Mengunduh daftar proxy terbaru dari semua sumber...")
    proxies_raw = []

    def _fetch_source(args):
        idx, source_url = args
        try:
            req = requests.get(source_url, timeout=10, headers=get_headers(), proxies={"http": None, "https": None})
            if req.status_code == 200:
                lines = [p.strip() for p in req.text.strip().split("\n") if p.strip()]
                if lines:
                    log(f"[PROXY-SEARCH] Sumber [{idx}]: {len(lines)} proxy ditemukan.")
                    return lines
        except Exception as e:
            log(f"[WARN] Sumber [{idx}] gagal: {e}, dilewati.")
        return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(PROXY_SOURCES)) as ex:
        futs = [ex.submit(_fetch_source, (i, url)) for i, url in enumerate(PROXY_SOURCES)]
        for fut in concurrent.futures.as_completed(futs):
            try:
                proxies_raw.extend(fut.result())
            except Exception:
                pass

    log(f"[PROXY-SEARCH] Total proxy mentah dari semua sumber: {len(proxies_raw)} entri.")
    if not proxies_raw:
        log("[PROXY-FAIL] Semua sumber proxy gagal diambil.")
        return None

    proxies_clean = []
    for raw in proxies_raw:
        norm = normalize_proxy(raw)
        if norm:
            proxies_clean.append(norm)

    if not proxies_clean:
        log("[PROXY-FAIL] Tidak ada proxy valid setelah normalisasi.")
        return None

    PROXY_POOL = proxies_clean
    PROXY_POOL_INDEX = 0
    log(f"[PROXY-SEARCH] {len(proxies_clean)} proxy valid. Memulai pencarian proxy aktif...")

    result = search_working_proxy_in_pool(0)
    if result:
        PROXY_POOL_INDEX, found = result
        SELECTED_PROXY = found
        log(f"[PROXY-OK] Selesai. Proxy terpilih [{PROXY_POOL_INDEX}/{len(PROXY_POOL)-1}]: {found}")
        return found
    else:
        log("[PROXY-FAIL] Tidak ada proxy yang merespons 200 OK.")
        SELECTED_PROXY = None
        return None

def search_working_proxy_in_pool(start_idx: int) -> Optional[Tuple[int, str]]:
    """Mencari proxy aktif di PROXY_POOL secara concurrent mulai dari start_idx."""
    global PROXY_POOL
    if not PROXY_POOL or start_idx >= len(PROXY_POOL):
        return None

    target_stats = "https://i.klikhost.com:8502/stats?json=1"
    found_proxy = None
    stop_event = threading.Event()

    def check_proxy(px_info):
        if stop_event.is_set():
            return None
        idx, px = px_info
        px_url = f"http://{px}"
        try:
            res = requests.get(
                target_stats,
                proxies={"http": px_url, "https": px_url},
                timeout=5,
                verify=False
            )
            if res.status_code == 200 and "streamstatus" in res.text:
                if not stop_event.is_set():
                    stop_event.set()
                    return (idx, px)
        except Exception:
            pass
        return None

    chunk_size = 500
    for chunk_start in range(start_idx, len(PROXY_POOL), chunk_size):
        chunk_end = min(chunk_start + chunk_size, len(PROXY_POOL))
        chunk = list(enumerate(PROXY_POOL[chunk_start:chunk_end], start=chunk_start))

        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
            futures = [executor.submit(check_proxy, item) for item in chunk]
            for future in concurrent.futures.as_completed(futures):
                res = future.result()
                if res:
                    found_proxy = res
                    try:
                        executor.shutdown(wait=False, cancel_futures=True)
                    except TypeError:
                        executor.shutdown(wait=False)
                    break

        if found_proxy:
            break

    return found_proxy

def next_proxy_from_pool() -> Optional[str]:
    """Beralih ke proxy berikutnya dari warm pool atau fallback scan PROXY_POOL."""
    global SELECTED_PROXY, PROXY_POOL_INDEX, PROXY_POOL
    with PROXY_WARM_LOCK:
        if PROXY_WARM_POOL:
            SELECTED_PROXY = PROXY_WARM_POOL.popleft()
            remaining = len(PROXY_WARM_POOL)
            log(f"[POOL] Beralih ke proxy hangat: {SELECTED_PROXY} (sisa warm pool: {remaining}/{PROXY_WARM_TARGET})")
            return SELECTED_PROXY

    if not PROXY_POOL:
        SELECTED_PROXY = None
        return None

    log("[PROXY-SEARCH] Warm pool kosong. Mencari proxy aktif berikutnya secara paralel...")
    result = search_working_proxy_in_pool(PROXY_POOL_INDEX + 1)
    if result:
        PROXY_POOL_INDEX, SELECTED_PROXY = result
        log(f"[PROXY-OK] Berhasil beralih ke proxy [{PROXY_POOL_INDEX}/{len(PROXY_POOL)-1}]: {SELECTED_PROXY}")
        return SELECTED_PROXY
    else:
        log("[PROXY-FAIL] Semua proxy dalam pool sudah dicoba/habis. Fallback ke koneksi langsung.")
        SELECTED_PROXY = None
        return None

def _proxy_warmer_loop():
    global PROXY_POOL_INDEX
    target_stats = "https://i.klikhost.com:8502/stats?json=1"
    scan_idx = PROXY_POOL_INDEX + 1

    while not _proxy_warmer_stop.is_set():
        with PROXY_WARM_LOCK:
            current_size = len(PROXY_WARM_POOL)

        if current_size >= PROXY_WARM_TARGET:
            _proxy_warmer_stop.wait(timeout=10)
            continue

        needed = PROXY_WARM_TARGET - current_size
        if not PROXY_POOL or scan_idx >= len(PROXY_POOL):
            scan_idx = 0

        stop_event = threading.Event()
        found_batch = []
        found_lock = threading.Lock()

        def _check(px_info):
            if stop_event.is_set():
                return
            idx, px = px_info
            px_url = f"http://{px}"
            try:
                res = requests.get(
                    target_stats,
                    proxies={"http": px_url, "https": px_url},
                    timeout=5,
                    verify=False,
                )
                if res.status_code == 200 and "streamstatus" in res.text:
                    with found_lock:
                        found_batch.append((idx, px))
                        if len(found_batch) >= needed:
                            stop_event.set()
            except Exception:
                pass

        chunk_end = min(scan_idx + 500, len(PROXY_POOL))
        chunk = list(enumerate(PROXY_POOL[scan_idx:chunk_end], start=scan_idx))
        scan_idx = chunk_end

        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as ex:
            futs = [ex.submit(_check, item) for item in chunk]
            concurrent.futures.wait(futs, timeout=15)

        if found_batch:
            with PROXY_WARM_LOCK:
                for _, px in found_batch[:needed]:
                    if px not in PROXY_WARM_POOL:
                        PROXY_WARM_POOL.append(px)
                new_size = len(PROXY_WARM_POOL)
            log(f"[POOL] Warm pool diisi +{len(found_batch[:needed])} proxy → {new_size}/{PROXY_WARM_TARGET} siap")

        if not _proxy_warmer_stop.is_set():
            _proxy_warmer_stop.wait(timeout=2)

def start_proxy_warmer():
    _proxy_warmer_stop.clear()
    t = threading.Thread(target=_proxy_warmer_loop, daemon=True, name="proxy-warmer")
    t.start()
    log(f"[POOL] Proxy warmer dimulai — target {PROXY_WARM_TARGET} proxy siap di latar belakang.")

def stop_proxy_warmer():
    _proxy_warmer_stop.set()

def next_proxy_parallel_from_pool(target_url=None, max_batch=8, timeout=5) -> Optional[str]:
    """Test hingga max_batch proxy dari warm pool secara paralel untuk fast reconnect."""
    global SELECTED_PROXY
    if not target_url:
        target_url = "https://i.klikhost.com:8502/stats?json=1"

    with PROXY_WARM_LOCK:
        batch = list(itertools.islice(PROXY_WARM_POOL, max_batch))

    if not batch:
        return next_proxy_from_pool()

    winner_holder = [None]
    winner_lock = threading.Lock()
    stop_event = threading.Event()

    def _check(px):
        if stop_event.is_set():
            return
        px_url = f"http://{px}"
        try:
            res = requests.get(
                target_url,
                proxies={"http": px_url, "https": px_url},
                timeout=timeout,
                verify=False,
            )
            if res.status_code == 200 and "streamstatus" in res.text:
                with winner_lock:
                    if winner_holder[0] is None:
                        winner_holder[0] = px
                        stop_event.set()
        except Exception:
            pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(batch)) as ex:
        futs = [ex.submit(_check, px) for px in batch]
        concurrent.futures.wait(futs, timeout=timeout + 2)

    winner = winner_holder[0]

    with PROXY_WARM_LOCK:
        for px in batch:
            try:
                PROXY_WARM_POOL.remove(px)
            except ValueError:
                pass
        remaining = len(PROXY_WARM_POOL)

    if winner:
        SELECTED_PROXY = winner
        log(f"[POOL] Parallel pick: {winner} terpilih dari {len(batch)} kandidat (sisa warm pool: {remaining}/{PROXY_WARM_TARGET})")
        return winner

    log(f"[PROXY-FAIL] Seluruh batch {len(batch)} proxy gagal. Mencari dari sisa pool...")
    return next_proxy_from_pool()
