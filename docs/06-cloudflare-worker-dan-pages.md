# 06 — Infrastruktur Cloudflare (Worker & Pages)

Bab ini mendokumentasikan peran **Cloudflare Worker** sebagai gerbang backend tunggal dan **Cloudflare Pages** sebagai media distribusi situs web Voice of Trisma.

---

## ⚡ Cloudflare Worker Gabungan (`voiceoftrisma`)

Seluruh backend serverless VOT disatukan ke dalam satu proyek Worker tunggal di:
📁 `cloudflare/workers/voiceoftrisma/`

Domain Produksi: **`https://voiceoftrisma.anandapradnyana68.workers.dev`**

### Peta Rute API (Endpoints Map):

```text
https://voiceoftrisma.anandapradnyana68.workers.dev
├── /              --> Status layanan & daftar endpoint
├── /stats         --> Proxy CORS live status Klikhost & time-series 6 jam D1
├── /archive       --> Cache katalog rekaman siaran dari Archive.org
├── /archive/download/* --> Reverse proxy download audio/file Archive.org (anti 502/rate-limit GHA)
├── /metrics       --> Snapshot statistik pendengar (aktif jam 15:00-19:00 WITA)
└── /api/*         --> Backend Admin & Pengelolaan Jadwal Siaran
    ├── /api/login   (Otentikasi admin via SESSION_SECRET HMAC)
    ├── /api/jadwal  (GET jadwal publik & PUT pembaruan jadwal admin)
    └── /api/logs    (Riwayat audit aktivitas admin)
```
*(Pemicu GitHub Actions berjalan murni via scheduled cron runtime internal — tanpa rute HTTP publik).*

---

## 🗄️ Database Cloudflare D1

Worker ini terhubung ke database SQLite serverless bawaan Cloudflare yaitu **Cloudflare D1**:
* **Database Name**: `voiceoftrisma`
* **Binding**: `env.DB`
* **Tabel Utama**:
  1. `samples`: Menyimpan riwayat statistik pendengar setiap 5 menit (dengan pembersihan otomatis data yang lebih tua dari 6 jam).
  2. `kv_store`: Menyimpan jadwal siaran harian, sesi login admin, dan cache arsip.

---

## 🔐 Manajemen Secrets Worker

Secrets diatur langsung di Cloudflare menggunakan Wrangler CLI:
```bash
cd cloudflare/workers/voiceoftrisma
npx wrangler secret put <NAMA_SECRET>
```

| Nama Secret | Fungsi | Nilai / Sifat |
| :--- | :--- | :--- |
| **`ADMIN_USERNAME`** | Username untuk login ke dashboard admin. | String teks (contoh: `mpvot`). |
| **`ADMIN_PASSWORD`** | Kata sandi untuk login ke dashboard admin. | String teks rahasia. |
| **`SESSION_SECRET`** | Kunci kriptografi HMAC untuk menandatangani token sesi admin. | String acak statis 32+ karakter. |
| **`GITHUB_TOKEN`** | Token Personal Access GitHub untuk memicu alur kerja Actions otomatis dari Worker Cron. | GitHub PAT dengan scope `workflow`. |

---

## 🌐 Cloudflare Pages (`voiceoftrisma.pages.dev`)

Situs web publik Voice of Trisma di-host di Cloudflare Pages sebagai alternatif berkecepatan tinggi dari GitHub Pages:

* **URL Publik**: `https://voiceoftrisma.pages.dev/`
* **Direktori Sumber di Repo**: `template/main/`
* **Metode Deployment**: *Direct Upload* via GitHub Actions.

### Alur Kerja Deployment Bersamaan (`.github/workflows/web-deploy.yml`):
Setiap kali ada pembaruan kode di branch `main`:
1. **Taking Resources**: File dari `template/main/` disiapkan ke direktori sementara `web/`.
2. **Push to Github Pages**: Meng-clone dan men-deploy ke repositori `voiceoftrisma.github.io`.
3. **Push to Cloudflare Pages**: Menjalankan action resmi `cloudflare/wrangler-action@v3` untuk mengunggah folder `web/` ke project `voiceoftrisma` menggunakan kredensial **`CF_TOKEN`** dan **`CLOUDFLARE_ACCOUNT_ID`**.
