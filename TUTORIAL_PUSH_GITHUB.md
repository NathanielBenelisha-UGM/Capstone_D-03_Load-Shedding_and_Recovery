# 📤 Tutorial Push Update ke GitHub

## 📋 Persyaratan Sekali Saja (Sudah Dilakukan)
- Git sudah terinstall ✅
- Remote `origin` sudah terhubung ke GitHub Anda ✅
- Login GitHub sudah tersimpan di Windows Credential Manager ✅

---

## 🔁 Cara Push Update (Setiap Kali Ada Perubahan)

Buka terminal di folder `e:\CAPSTONE-NIEL`, lalu jalankan **3 perintah ini secara berurutan**:

### Langkah 1 — Tandai semua perubahan
```bash
git add .
```
> Perintah ini memasukkan semua file yang berubah ke "antrian" push.

### Langkah 2 — Beri nama/catatan perubahan
```bash
git commit -m "Tulis deskripsi singkat perubahan Anda di sini"
```
> Contoh nyata:
> ```bash
> git commit -m "fix: perbaiki crash load.py saat PLC offline"
> git commit -m "feat: tambah toggle switch AUTO/MANUAL di Load Settings"
> git commit -m "docs: update README dengan penjelasan MILP"
> ```

### Langkah 3 — Upload ke GitHub
```bash
git push
```
> Perubahan Anda sekarang live di GitHub!

---

## ✅ Contoh Sesi Lengkap

```bash
cd e:\CAPSTONE-NIEL

git add .
git commit -m "feat: tambah kolom priority di tabel load"
git push
```

Output yang berarti **sukses**:
```
[main a1b2c3d] feat: tambah kolom priority di tabel load
 3 files changed, 45 insertions(+), 12 deletions(-)
To https://github.com/NathanielBenelisha-UGM/Capstone_D-03_Load-Shedding_and_Recovery.git
   829c7ab..a1b2c3d  main -> main
```

---

## ⚠️ Masalah Umum & Solusinya

### ❌ `nothing to commit, working tree clean`
> Tidak ada file yang berubah sejak commit terakhir. Tidak perlu push.

### ❌ `! [rejected] main -> main (fetch first)`
> GitHub punya perubahan yang belum Anda punya. Jalankan:
> ```bash
> git pull --rebase origin main
> git push
> ```

### ❌ `fatal: not a git repository`
> Anda berada di folder yang salah. Pastikan terminal berada di `e:\CAPSTONE-NIEL`:
> ```bash
> cd e:\CAPSTONE-NIEL
> ```

### ❌ `error: src refspec main does not match any`
> Belum ada commit sama sekali. Jalankan langkah 1 & 2 dulu, baru push.

---

## 💡 Tips Konvensi Nama Commit (Opsional tapi Rapi)

| Prefix | Kapan digunakan |
|--------|-----------------|
| `feat:` | Menambah fitur baru |
| `fix:` | Memperbaiki bug |
| `docs:` | Mengupdate dokumentasi/README |
| `refactor:` | Merapikan kode tanpa mengubah fungsi |
| `style:` | Mengubah tampilan/CSS |

---

## 🔗 Link Repository
[https://github.com/NathanielBenelisha-UGM/Capstone_D-03_Load-Shedding_and_Recovery](https://github.com/NathanielBenelisha-UGM/Capstone_D-03_Load-Shedding_and_Recovery)
