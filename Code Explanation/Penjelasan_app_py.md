# Bedah Kode: `app.py` (SCADA Master)

File `app.py` adalah otak sentral dari proyek Capstone Anda. File ini membentang sepanjang kurang lebih 600 baris. Berikut adalah penjelasan fungsionalitasnya per blok kode (atau *section*):

## Blok 1: Import Pustaka (Baris 1 - 13)
*   **Baris 1-2 (`flask`, `flask_socketio`):** Menyiapkan *server* Web dan *WebSocket* (Socket.IO). Socket.IO penting agar data bisa mengalir *real-time* ke HMI tanpa perlu *refresh* halaman.
*   **Baris 3 (`ModbusTcpClient`):** Ini adalah *driver* komunikasi industri untuk berbicara dengan PLC Schneider.
*   **Baris 4-7 (`threading`, `time`, `pulp`, `loadflow_module`):** Mengimpor pustaka untuk membuat proses berjalan di latar belakang (*thread*), penghitung waktu, pustaka algoritma optimasi (PuLP), dan pemanggil simulasi *Load Flow* eksternal.

## Blok 2: Konfigurasi PLC & Memori (Baris 15 - 75)
*   **Baris 18-20:** Mendefinisikan alamat IP dan Port PLC (Default: `192.168.1.13:502`).
*   **Baris 35-38 (`ADDR_...`):** Ini adalah **Peta Harta Karun** Anda. SCADA harus tahu di alamat laci mana data disimpan di PLC (misal laci `0` untuk beban, laci `30` untuk generator, laci `54` untuk frekuensi).
*   **Baris 45-50 (`GEN_MAP`):** Sebuah *dictionary* yang memetakan generator ke alamat sensor (untuk membaca) dan alamat koil (untuk menyuruh *trip*).
*   **Baris 54-70 (`LOADS`):** Daftar seluruh 12 beban masyarakat beserta tipe prioritas awalnya (2 = *Non-Essential*, 3 = *Essential*, 4 = *Critical*). Beban inilah yang nanti akan dipilih oleh algoritma untuk dibunuh.
*   **Baris 72 (`MILP_PRIORITY_WEIGHTS`):** Ini adalah rumus rahasia MILP Anda. Beban prioritas 2 diberi "harga/bobot" murah (1), sedangkan prioritas 4 diberi harga sangat mahal (100). Tujuannya agar MILP enggan memutus beban prioritas 4 karena algoritmanya mencari "harga pemutusan termurah" (*Minimize Cost*).

## Blok 3: Jantung Optimasi `solve_milp_shedding` (Baris 80 - 146)
Ini adalah fungsi matematika terpenting di proyek ini.
*   **Baris 84 (`time.perf_counter()`):** Mulai menghitung waktu *stopwatch* dengan presisi perangkat keras (mikrodetik).
*   **Baris 86-90:** Membuat wadah variabel biner (`0` atau `1`) untuk tiap beban. `1` artinya beban tersebut dipilih untuk diputus.
*   **Baris 92-93:** Memasukkan *Constraint* (Syarat Wajib) $\rightarrow$ Total MW dari beban yang dipilih nilainya harus **lebih besar atau sama dengan** Defisit Daya yang terjadi.
*   **Baris 95-106:** Membangun *Objective Function* (Fungsi Tujuan). Mengalikan variabel tadi dengan "bobot prioritas". Jika beban tersebut baru saja mati di detik sebelumnya, bobotnya didiskon 10% (baris 102). Ini disebut strategi *Anti-Oscillation* agar algoritma tidak bingung dan memutus-nyalakan beban yang sama terus-menerus.
*   **Baris 108-109 (`prob.solve(solver)`):** Memanggil *Engine* C++ `CBC` milik PuLP untuk menghitung rumus-rumus di atas.
*   **Baris 125-144:** Blok ini hanya dieksekusi jika `freq_hz` sedang kritis. Bertugas mencetak teks laporan yang megah (*Pretty Print*) ke layar Terminal (menampilkan waktu eksekusi milidetik, beban yang jadi korban, dll).

## Blok 4: Mata & Telinga SCADA `background_monitoring` (Baris 159 - 411)
Ini adalah siklus pemantauan abadi (*while True*) yang bekerja 10 kali per detik.
*   **Baris 169-237:** Fase "Baca Sensor". SCADA menghubungi PLC untuk membaca *register* generator, frekuensi, nilai beban aktual, dan jam.
*   **Baris 240-244:** Fase "Kalkulasi". Menghitung apakah `Total Beban > Kapasitas Generator yang Hidup`. Jika iya, didapatlah angka `capacity_deficit`.
*   **Baris 254-272 (Skenario Kritis):** Jika Frekuensi anjlok di bawah `49.0` Hz! Fungsi ini buru-buru memanggil `solve_milp_shedding(...)` dan mendelegasikan hasil putusannya (nama-nama beban) ke daftar `shed_set`.
*   **Baris 274-288 (Skenario Reprioritisasi):** Jika frekuensi normal, tapi generator kekurangan daya pelan-pelan (atau Anda tiba-tiba mengubah rumah sakit menjadi non-esensial via HMI), ia secara diam-diam memanggil MILP lagi tanpa membuat panik layar Terminal.
*   **Baris 290-330 (Skenario Pemulihan / *Smart Margin Restoration*):** Jika sistem sudah aman dan frekuensi stabil $> 49.95$ Hz. SCADA akan mencoba memulihkan beban yang mati satu per satu. **Trik Cerdasnya:** SCADA memiliki fitur pengecekan `is_settled` (memastikan nilai aktual beban yang baru direstorasi tidak kurang dari 90% target potensial dayanya sebelum menyalakan beban lain) dengan tambahan *timer* pemulihan minimal 1 detik (10 siklus). SCADA juga secara cerdas menghitung *Kapasitas Efektif* menggunakan margin batas tarikan sesaat **+20 MW** dari *Daya Riil* generator saat ini.
*   **Baris 332-342 (Skenario Prediktif/N-1):** Ini adalah tingkat kecerdasan lanjut. Meskipun sistem sedang aman, SCADA sudah meramal: *"Bagaimana kalau PLTA tiba-tiba mati detik depan?"*. Ia memanggil MILP secara simulasi untuk tiap generator. Hasilnya (*Contingency Matrix*) disiapkan jauh-jauh hari di memori.
*   **Baris 348-360 (Eksekusi Fisik):** Sinyal mematikan tegangan! Daftar `shed_set` dikirimkan secara massal kembali ke PLC melalui perintah `client.write_coil`. Di detik inilah lampu di lapangan benar-benar mati.
*   **Baris 362-388 (Load Flow Visual):** Memanggil file `loadflow_module.py` untuk menggambar ulang grafik Aliran Daya AC secara *background* jika ada beban yang mati, agar tidak membekukan (*freeze*) kinerja *looping* utama SCADA.
*   **Baris 395-407 (`socketio.emit`):** Membungkus semua kekacauan, status, dan angka tadi ke dalam paketan bernama `grid_update`, lalu dilempar ke *browser web* (HMI) agar bisa divisualisasikan oleh JavaScript.

## Blok 5: Rute Komunikasi API dari Web (Baris 417 - 595)
Ini adalah kumpulan fungsi penerima *request* ketika Anda mengklik tombol-tombol di layar Web (HMI).
*   **Baris 426 (`gen_control`):** Menangkap klik tombol *Trip* Generator di Web, lalu memerintahkan Coil PLC untuk mati.
*   **Baris 455 (`set_load_interrupt`):** Menangkap *slider* manipulasi daya beban di Web (Override channel). Mengirim nilainya ke laci khusus di PLC (`ADDR_OVERRIDE`).
*   **Baris 489 (`set_gen_interrupt`):** Menangkap nilai manual daya (MW) masing-masing generator dari HMI untuk mengabaikan kontrol otomatis (Override Generator).
*   **Baris 541 (`set_load_priority`):** Menangkap menu geser prioritas beban di Web. Sangat keren karena fungsi ini tidak hanya mengubah memori sementara, tapi juga menjalankan *RegEx* (baris 525-540) untuk **mengedit paksa teks tulisan** di dalam file `app.py` itu sendiri secara langsung. Hal ini membuat prioritas baru tersebut menjadi permanen (*persist*) walaupun PC direstart.

## Penutup
Baris terakhir `socketio.run` menjalankan *server* di `localhost:5000`. Jika SCADA ini adalah sebuah tubuh, `background_monitoring` adalah sistem saraf refleks otonomnya, sedangkan `solve_milp_shedding` adalah otak cerdasnya.
