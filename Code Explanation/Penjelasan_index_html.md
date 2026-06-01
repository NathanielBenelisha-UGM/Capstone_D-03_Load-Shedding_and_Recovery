# Bedah Kode: `index.html` (Kerangka Wajah HMI)

File `index.html` (terletak di folder `templates/`) adalah **kerangka tulang** dari seluruh antarmuka web SCADA Anda. Jika `main.js` adalah otot yang menggerakkan, dan `style.css` adalah kulitnya, maka file ini adalah struktur rangkanya.

Semua tombol, tabel, panel kaca, dan ruang grafik yang Anda lihat di layar didefinisikan tempatnya di dalam file ini.

Berikut adalah penjelasan fungsional per blok kode:

## Blok 1: Kepala Dokumen & Pustaka (Baris 1 - 22)
*   **Baris 10-12:** Memuat (*import*) huruf modis dari Google Fonts. `Outfit` untuk teks biasa, `Playfair Display` untuk judul elegan, dan `JetBrains Mono` untuk angka-angka digital seperti di ruang kendali (*Control Room*).
*   **Baris 15-18:** Memuat pustaka eksternal. `three.js` untuk grafik latar belakang 3D, `chart.js` untuk grafik garis, `socketio.js` untuk komunikasi *real-time*, dan `@phosphor-icons` untuk memunculkan ikon-ikon cantik.

## Blok 2: Latar Belakang 3D & Efek Antigravitasi (Baris 26 - 35)
*   **Baris 27:** `<canvas id="bg-canvas">` adalah kanvas tempat `three.js` akan memutar bintang-bintang debu angkasa yang berputar.
*   **Baris 35:** `<canvas id="bubble-canvas">` adalah lapisan tempat gelembung warna-warni yang "takut" pada *mouse* Anda (*Antigravity Bubbles*) akan dirender.

## Blok 3: Layar Selamat Datang / *Welcome Screen* (Baris 31 - 71)
*   Tirai hitam raksasa (`#welcome-screen`) yang menutupi layar saat pertama kali di-*refresh*.
*   **Baris 44-49:** Menampilkan judul Skripsi / Capstone Anda yang sangat panjang. Ukurannya dibuat dinamis menggunakan CSS `clamp(...)` agar tidak berantakan di layar kecil.
*   **Baris 56-61:** Tombol masuk sebagai *Viewer* (hanya menonton) atau *Admin Login*. Jika tombol Admin ditekan, ia akan memanggil kotak abu-abu yang meminta *password* (`admin123`).

## Blok 4: Skrip Visual Antigravitasi Murni (Baris 73 - 157)
*   Ini adalah skrip *JavaScript* yang sengaja ditaruh di dalam HTML untuk membangun partikel warna-warni (merah, kuning, hijau, biru) yang melayang di *Welcome Screen*.
*   **Baris 120-126:** Terdapat kalkulasi fisika buatan (hukum tolak menolak). Jika kursor *mouse* Anda mendekati partikel kurang dari jarak tertentu (`maxDistance = 200`), partikel tersebut akan "didorong" menjauh (efek repulsi / *Antigravity*).

## Blok 5: Kerangka Tata Letak Utama HMI (Baris 159 - Akhir)
Setelah Anda *login*, layar akan menampakkan struktur ini:

*   **`#sidebar` (Baris 162-201):**
    Menu navigasi di sisi kiri layar. Setiap tombol (`.nav-item`) memiliki fungsi `data-tab` yang mengacu pada "halaman/panel" mana yang akan dimunculkan di sebelah kanan.

*   **`#header` (Baris 207-222):**
    Palang bagian atas yang selalu muncul. Menampilkan judul di kiri, dan detak **Jam PLC** serta **Status PLC** di ujung kanan.

*   **Ruang Panel / *Tab Panes* (Baris 226 - 491):**
    Ini adalah isi dari masing-masing menu.
    *   **Tab Overview (`#tab-overview`):** Menampung empat kartu angka raksasa (Total Gen, Load, Deficit, Freq) serta menyisakan div kosong (`<canvas id="freqChart">`) untuk diisi grafik oleh `main.js`.
    *   **Tab Load Settings (`#tab-load-settings`):** Menampung tabel status beban (hidup/mati). Khusus untuk Anda (*Admin*), ada panel tambahan untuk mengubah nilai paksa (*Override Sensor*).
    *   **Tab Generator (`#tab-gen-settings`):** Menampung tabel kapasitas generator dan tombol saklar *Trip* paksa (ON/OFF).
    *   **Tab Contingency (`#tab-contingency`):** Matriks tebakan N-1 yang memprediksi siapa saja yang akan mati jika salah satu generator meledak.
    *   **Tab Bus & Priority (`#tab-group-def`):** Di sini Anda (sebagai Admin) dapat mengubah prioritas rumah sakit dari `4` (Kritis) menjadi `2` (Non-esensial).
    *   **Tab Alarm & History (`#tab-alarm-log`):** Kotak panjang sederhana untuk menampung riwayat teks (*log*) yang mengalir dari bawah ke atas.
    *   **Tab SLD & Heatmap (`#tab-sld`):** Tab pamungkas. Memuat pembungkus kosong `<div id="sld-container">` yang nanti akan disuntikkan file gambar `SLD.svg` oleh `main.js`. Di sebelahnya ada kartu vertikal untuk menaruh skor terburuk dari algoritma Aliran Daya (Tegangan Terendah & *Loading* Kabel Maksimal).

## Blok 6: Efek CSS Khusus SVG (Baris 443 - 490)
Gaya CSS ini sengaja ditempel di sini agar bisa "menembus" masuk ke dalam bayangan gambar *Single Line Diagram*.
*   **`.gen-pulse`:** Animasi agar lingkaran simbol generator di peta membesar-mengecil berdenyut secara otomatis memancarkan cahaya hijau.
*   **`.gen-spin`:** Animasi agar huruf `~` di dalam simbol generator berputar 360 derajat seperti baling-baling kincir.
*   **`.energy-flow`:** Animasi pada garis kabel (menggunakan *stroke-dasharray*) sehingga terlihat seperti aliran elektron yang merambat di sepanjang kawat saat jaringan aktif.
