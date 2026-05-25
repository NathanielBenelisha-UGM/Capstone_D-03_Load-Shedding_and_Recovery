# Dokumentasi SCADA Master & Web HMI

Folder ini menaungi sistem saraf pusat (*Central Nervous System*) dari arsitektur *Dynamic Load Shedding*. Sistem ini dibangun dengan kombinasi **Python (Backend)**, **Flask + Socket.IO (Middleware)**, dan **HTML/JS/CSS (Frontend HMI)**.

## 1. Algoritma Optimasi Pelepasan Beban (UFLS)

Jika frekuensi grid (seperti yang dihitung oleh *Physics Engine*) jatuh menyentuh ambang batas kritis ($\le 49.50$ Hz), SCADA tidak lagi memutus saluran listrik secara statis/buta. Sistem ini memformulasikan masalah pemadaman menjadi persoalan optimasi *Mixed-Integer Linear Programming* (MILP) menggunakan *library* `PuLP`.

### 1.1 Formulasi Matematis MILP
Tujuan (*Objective*): Meminimalisir dampak kerugian akibat pemadaman berdasar level utilitas.
*   **Variabel Keputusan ($x_i$):** Bernilai biner. $x_i = 1$ berarti beban dipertahankan, $x_i = 0$ berarti dipadamkan.
*   **Fungsi Objektif:**
    $$ \min \sum_{i=1}^{N} (1 - x_i) \cdot P_i \cdot W_i $$
    Di mana $P_i$ adalah daya (MW) aktual dari beban tersebut, dan $W_i$ adalah bobot kepentingan fasilitas (Prioritas 4 = Sangat Penting, berbobot 100x lebih mahal untuk dipadamkan daripada Prioritas 2).

*   **Kendala Kelistrikan (*Constraint*):**
    Total daya beban yang dikorbankan ($x_i=0$) harus memenuhi besaran *Capacity Deficit*.
    $$ \sum_{i=1}^{N} (1 - x_i) \cdot P_i \ge P_{Defisit} $$

### 1.2 *Anti-Oscillation* & *Dynamic Priority Swapping*
*   **Osilasi Breaker:** Terjadi jika dua beban (L1 dan L2) berprioritas sama. Algoritma terus menerus menukar pemadaman L1 dan L2 tiap siklus detik, mengakibatkan kerusakan alat fisik.
*   **Penyelesaian:** Algoritma menerapkan Diskon Objektif 10% ($W_i \times 0.90$) pada bobot biaya beban yang *sedang* dalam kondisi padam. Ini mencegah pertukaran bodoh antara dua prioritas yang sama.
*   **Pertukaran Dinamis:** Meskipun terkunci, jika operator lewat HMI menaikkan level utilitas beban padam menjadi "Kritis", algoritma secara instan akan memprioritaskannya lagi, mengalahkan diskon tersebut, dan merestorasi bebannya sembari menumbalkan beban prioritas rendah lain.

## 2. Backend Arsitektur (app.py)

Skrip `app.py` bertindak sebagai *SCADA Master Poller* sekaligus Web Server.
*   **Modbus TCP Client (`pymodbus`)**: Berlari dalam *thread* terpisah secara asinkron dengan *polling rate* 10 Hz (100ms) untuk mengambil memori Word (`%MW`) dari Virtual PLC. 
*   **Multi-threading**: Memisahkan antrean komputasi MILP yang intensif CPU dengan pengiriman Socket.IO agar antarmuka tidak tertunda (*hang*).
*   **Matriks Kontingensi (N-1):** Selalu menghitung terlebih dahulu (*predictive*) konsekuensi andai kata generator terbesar jatuh seketika, menghasilkan *Preselection* target warna merah pada HMI.

## 3. Frontend Control Room (index.html, main.js, styles.css)

HMI Control Room didesain menggunakan **Vanilla CSS (Glassmorphism)** untuk memberikan kesan panel sistem cerdas yang modern.

*   **Socket.IO Client:** Menerima `grid_update` pada kecepatan *high-frequency*.
*   **Chart.js (Transient Frequency Plotting):** Menggambar deret waktu frekuensi listrik ($f$ pada sumbu Y, $t$ pada sumbu X). Chart.js diperbarui dinamis menggunakan *circular buffer* berkapasitas tertentu agar RAM *browser* tidak *overflow*.
*   **Interactive Single Line Diagram (SLD):** Jalur dan warna *feeder* menyala (hijau) atau padam (merah) menggunakan manipulasi DOM SVG secara *real-time* sesuai status coil PLC `%M60`.
*   **Event Tracker (History Log):** Algoritma *frontend* menyaring notifikasi log. Pesan rutin diabaikan, dan hanya mencatat *Incident Event* permanen (Pemadaman, UFLS Trigger, Restorasi) sehingga operator dapat menelaah jejak rekam insiden.
