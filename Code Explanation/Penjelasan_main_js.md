# Bedah Kode: `main.js` (Logika *Frontend* / HMI Web)

File `main.js` yang berada di dalam folder `static/` adalah jembatan antara layar peramban (*browser*) Anda dengan otak SCADA (`app.py`). File ini ditulis menggunakan JavaScript murni (Vanilla JS) dengan gaya yang sangat modern.

Jika `app.py` adalah otak matematisnya, maka `main.js` adalah **wajah dan indera perabanya**. Berikut adalah bedah fungsionalitas dari setiap blok utama di dalam file ini:

## Blok 1: Inisialisasi & Keamanan (Baris 1 - 52)
*   **Baris 8-9:** Mendeteksi parameter URL `?role=admin`. Jika Anda *login* tanpa parameter ini, Anda hanya menjadi *Viewer* (penonton).
*   **Baris 12-37 (`enterDashboard`):** Logika layar *Welcome Screen*. Jika Anda memilih "Admin Mode", ia akan meminta *password* sederhana (`admin123`). 
*   **Baris 39-52 (`DOMContentLoaded`):** Saat HMI pertama kali dibuka, ia akan membangunkan semua komponen secara berurutan: membangun latar belakang 3D, memanggil koneksi WebSockets, merakit grafik, dan memuat gambar kelistrikan (SLD).

## Blok 2: Efek Visual Latar Belakang (Baris 54 - 94)
*   **`init3DBackground()`:** Fungsi ini menggunakan *library* **Three.js** untuk me-*render* efek partikel melayang (bintang/debu kosmik) berwarna biru *cyan* (`0x00f3ff`). Animasi ini diputar menggunakan GPU komputer Anda melalui `WebGLRenderer` sehingga antarmuka HMI Anda terlihat sekelas sistem masa depan (*Sci-Fi*).

## Blok 3: Grafik Transien Frekuensi (Baris 115 - 144)
*   **`initFreqChart()`:** Membangun *Line Chart* menggunakan *library* **Chart.js**. Grafik ini dialokasikan untuk menampung data riwayat frekuensi. Area Y disetel kaku dari 48.0 Hz sampai 52.0 Hz agar saat frekuensi anjlok, visualisasinya terlihat jelas melorot dari garis tengah.

## Blok 4: Logika Peta SLD Interaktif (Baris 146 - 224)
Ini adalah fitur *frontend* yang paling rumit. File SVG statis diubah menjadi denah interaktif.
*   **`loadSVG()` / `initSLD()`:** Ia tidak menampilkan gambar SVG layaknya gambar `<img src="...">` biasa, melainkan menyuntikkan (*inject*) *source code* SVG tersebut ke dalam *tag* HTML (`container.innerHTML = svgText`). Tujuannya? Agar JavaScript bisa "menyentuh" dan mewarnai kabel-kabel di dalamnya satu per satu.
*   **Baris 156-200 (`initPanZoom`):** Menambahkan fungsi untuk menyeret layar (*Drag/Pan*) menggunakan *mouse* (dengan kalkulasi sumbu X & Y) dan melakukan *Zoom In / Zoom Out* (mendengarkan aksi roda / *scroll wheel* pada *mouse*).

## Blok 5: Penghubung Soket / WebSockets (Baris 319 - 389)
*   **`initSocket()`:** Di sini `main.js` mengetuk pintu `app.py`.
*   **Baris 333-342 (`socket.on('grid_update')`):** Ini adalah fungsi pendengar (*listener*). Ingat *looping* 10 kali per detik di `app.py`? Setiap kali `app.py` menembakkan paket data, blok kode inilah yang menangkapnya!
    Begitu paket data tiba, ia membaginya ke fungsi-fungsi kecil:
    *   `updateOverview(data)`: Mengubah angka statistik besar di atas.
    *   `updateGenerators(data)`: Mengubah tabel dan *bar chart* generator.
    *   `updateLoads(data)`: Mengubah warna tabel prioritas beban.
    *   `updateContingency(data)`: Memperbarui matriks tebakan pemutusan (N-1).
    *   `updateSLD(data)`: Merubah warna kabel di gambar SLD menjadi menyala terang hijau jika hidup, atau kelap-kelip merah jika kena pemutusan.
*   **Baris 344-388 (`socket.on('live_loadflow_result')`):** Pendengar khusus untuk paket data dari `loadflow_module.py`. Ia akan mengekstrak persentase *loading* trafo dan tegangan terendah untuk ditampilkan ke *Dashboard* sebelah kanan. Jika ada peringatan *Overload*, teksnya otomatis diubah merah.

## Blok 6: Mesin Pewarna / *Heatmap Rendering* (Baris 228 - 317)
*   **`applyHeatmapToSLD(d, scadaData)`:** Ini adalah fungsi pasca-pemrosesan (*post-processing*). Setelah data Aliran Daya AC diterima, fungsi ini mencari kawat (*Line*), Trafo, atau *Bus* (Terminal) yang kelebihan muatan.
    *   Jika kabel dialiri $> 80\%$, kawat diubah menjadi kuning kemerahan (`#ffca28`).
    *   Jika dialiri $> 100\%$ (Kritis), kawat langsung berubah warna merah darah (`#ff4444`) beserta efek *drop-shadow* (bercahaya) di sekeliling kawatnya.
    *   **Pengecualian Bus Mati:** Terdapat sinkronisasi brilian antara `scadaData` (Status Generator) dengan fungsi ini. Jika seluruh generator di sebuah bus mati, `main.js` akan menolak menggambar peringatan *Heatmap* (merah) pada bus tersebut dan mempertahankannya berwarna abu-abu gelap, menyempurnakan visualisasi *Blackout*.

## Blok 7: Sistem Pengiriman Data ke Backend
Meskipun tidak ditampilkan penuh di potongan atas, terdapat fungsi-fungsi seperti `toggleGen(name, action)` atau `sendSinglePriority(id, value)` (terletak di bagian bawah file).
Fungsi-fungsi ini bertugas bereaksi jika Anda mengeklik tombol di layar, lalu membungkus perintah Anda, dan menembakkannya menggunakan `socket.emit(...)` agar `app.py` bisa mengeksekusi perintah tersebut ke PLC perangkat keras yang asli.

---

**Intisari:** File `main.js` tidak mengandung perhitungan kelistrikan. Tugasnya murni mengambil data berat dari *Backend*, mendandaninya, lalu mewarnai layar monitor Anda agar semewah mungkin (dengan animasi, grafik responsif, dan *glow-effect* pada kabel) agar dosen penguji terpukau melihat visualnya.
