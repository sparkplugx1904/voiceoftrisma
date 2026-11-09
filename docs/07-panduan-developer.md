# 07 — Panduan Pengembang (Local Runbook & Maintenance)

Dokumen ini adalah panduan praktis bagi programmer untuk menjalankan, menguji, dan memelihara sistem Voice of Trisma di komputer lokal (Windows atau Linux) tanpa memerlukan asisten AI.

---

## 💻 1. Persyaratan Awal (Prerequisites)
* **Python**: Versi 3.10 atau lebih baru.
* **FFmpeg**: Sudah tersedia otomatis di repositori ini (`bin/win-x64/` untuk Windows dan `bin/linux-x64/` untuk Linux).
* **Git** & **Git LFS**: Untuk mengunduh binary berukuran besar (`ffmpeg`, `ffprobe`).

---

## 🚀 2. Instalasi Dependensi Lokal

Jalankan skrip setup otomatis yang sudah disediakan di folder `main/`:

```bash
# Untuk keperluan perekaman audio saja
python main/setup.py record

# Untuk keperluan transkripsi audio AI
python main/setup.py transcript

# Untuk keperluan deskripsi Qwen LLM dan pengunggahan Archive.org
python main/setup.py descript

Atau instal langsung menggunakan pip:
```bash
python -m pip install -r requirements/record
python -m pip install -r requirements/transcript
python -m pip install -r requirements/descript

---

## 🔑 3. Konfigurasi Berkas `.ENV` Lokal

Buat atau sunting berkas **`.ENV`** di root repositori (jangan taruh di dalam folder `main/`):

```env
# Kredensial Archive.org S3
IA_ACCESS_KEY="kunci_access_archive_anda"
IA_SECRET_KEY="kunci_secret_archive_anda"

# URL Stream Radio
STREAM_URL="http://i.klikhost.com:8502/stream"

# Google Gemini API (untuk descriptor)
GEMINI_API_KEY="kunci_api_gemini_anda"

# Daftar Sumber Proxy (Wajib Format JSON Array)
HTTP_PROXY='["https://raw.githubusercontent.com/iplocate/free-proxy-list/refs/heads/main/protocols/http.txt"]'
```

*Catatan:* Script Python sudah dirancang otomatis mencari berkas `.ENV` di root repositori, di mana pun Anda menjalankan terminal.

---

## 🧪 4. Resep Pengujian Lokal (Local Testing Recipes)

### A. Uji Coba Perekaman Tanpa Upload (Safe Dry-Run)
Gunakan flag `--skip-check` (lewati tunggu siaran) dan `--no-upload` (jangan unggah ke Archive.org):

```bash
# Rekam selama 30 detik saja untuk memastikan FFmpeg bekerja
python main/record_v2.py --skip-check --no-record --no-upload --duration 30
```

### B. Uji Coba Perekaman Siaran Nyata
```bash
# Merekam siaran langsung dari Klikhost dan disimpan di recordings/
python main/record_v2.py --no-upload
```

### C. Uji Coba Transkripsi Whisper Secara Lokal
```bash
# Transkripsi audio lokal menggunakan model small
python main/transcribe.py recordings/VOT-Denpasar_contoh.mp3 small
```

### D. Uji Coba Pembuatan Deskripsi Gemini AI
```bash
python main/descriptor.py transcripts/transcript.srt -o transcripts/description.json
```

### E. Uji Coba Pipeline Guard
```bash
# Cek status kesehatan 5 rekaman terakhir di Archive.org
python main/guard.py status --rows 5
```

---

## ❓ 5. Panduan Pemecahan Masalah (Troubleshooting)

### Kasus 1: `[ERROR] API key belum diatur`
* **Penyebab**: `IA_ACCESS_KEY` atau `IA_SECRET_KEY` belum terisi di berkas `.ENV`.
* **Solusi**: Pastikan berkas `.ENV` berada di root direktori dan sudah terisi string API key yang benar.

### Kasus 2: `HTTP/1.0 401 Unauthorized` dari Klikhost
* **Penjelasan**: Ini **bukan error kredensial**. Server Shoutcast/Klikhost memang mengembalikan HTTP 401 saat radio sedang **OFF-AIR** (tidak ada penyiar yang sedang on-air).
* **Solusi**: Ini perilaku normal. Recorder akan otomatis menunggu dan masuk ke mode *polling wait* hingga siaran menyala (*streamstatus: 1*).

### Kasus 3: `ImportError: No module named 'core'`
* **Solusi**: Seluruh skrip di `main/` sudah memiliki injeksi `sys.path` otomatis. Jika menjalankan skrip kustom sendiri, pastikan menyertakan:
  ```python
  import sys
  from pathlib import Path
  sys.path.insert(0, str(Path(__file__).resolve().parent))
  ```

---

## 📏 6. Checklist Pemeliharaan Kode untuk Pengembang Manusia

Jika Anda ingin menambahkan fitur baru di masa depan:
1. **Aturan DRY**: Jika sebuah fungsi akan dipakai oleh lebih dari 1 file, letakkan di paket **`main/core/`**, jangan di-*copy-paste*.
2. **Hindari Titik pada Nama File Python**: Gunakan `snake_case.py` (contoh: `record_v1.py`, bukan `record_v1.0.py`).
3. **Lengkapi Type Hints**: Selalu tentukan tipe data argumen fungsi (`def foo(bar: str) -> bool:`).
4. **Validasi Sintaks**: Selalu jalankan `python -m py_compile main/*.py` sebelum melakukan git commit.
