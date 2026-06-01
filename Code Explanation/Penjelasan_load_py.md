# Bedah Kode: `load.py` (Simulator Fisika & Lingkungan)

File `load.py` berperan sebagai **Mesin Fisika Alam Semesta** di proyek Anda. SCADA (`app.py`) tidak akan punya kerjaan jika tidak ada file ini, karena `load.py` lah yang bertugas membuat kekacauan, menggerakkan jarum jam, menaik-turunkan beban masyarakat, dan meruntuhkan frekuensi.

Secara garis besar, file ini terus-menerus berjalan (*looping* setiap 0.1 detik) untuk menyuntikkan data-data kelistrikan murni ke dalam otak PLC.

Berikut adalah penjelasan fungsionalitasnya per blok kode:

## Blok 1: Peta Modbus & Konfigurasi Karakteristik (Baris 1 - 85)
*   **Baris 15-40 (Peta Memori & Coil):** Sama seperti di `app.py`, ini adalah kompas alamat laci PLC. Bedanya, `load.py` lebih banyak bertugas **menulis** (`write`) ke laci sensor daripada membaca. Ia mendefinisikan posisi Coil PLC untuk tiap generator dan beban.
*   **Baris 42-45 (Konfigurasi Beban):** Array batas maksimal (MW) dari ke-12 beban yang Anda miliki (misal L101 maksimal 20 MW). 
*   **Baris 47-83 (`GEN_CFG`):** Ini adalah data **DNA Fisika** dari masing-masing generator:
    *   `rated` & `min`: Batas atas dan bawah daya keluaran.
    *   `H` (Konstanta Inersia): Nyawa utama sistem AC. PLTA (Hydro) punya beban putar turbin yang sangat berat ($H=5.0$), sehingga sulit goyah. PLTS (Solar) tidak punya beban putar alami, jadi kita berikan Inersia Virtual ($H=0.5$).
    *   `droop`: Karakteristik respons generator terhadap perubahan frekuensi (Primary Reserve).
    *   `ramp`: Seberapa cepat generator bisa dinaikkan dayanya (PLTA pelan, PLTS sangat cepat).

## Blok 2: Fungsi Alam (Baris 87 - 110)
*   **Baris 90-99 (`get_load_pct`):** Kurva WBP/LWBP. Menghasilkan angka pecahan (0.0 - 1.0) berdasarkan jam simulasi untuk meniru pola konsumsi listrik masyarakat. Malam hari (WBP) nilainya mendekati 0.90, tengah malam anjlok ke 0.41.
*   **Baris 104-110 (`get_solar_factor`):** Fungsi matahari buatan. Menggunakan kurva sinus (terbit jam 5.5, tenggelam jam 18.5) dikalikan dengan faktor awan (`cloud`) yang diacak (`random.uniform`).

## Blok 3: Inisialisasi & Persiapan Awal (Baris 112 - 156)
*   **Baris 119-134:** Sangat Krusial! Saat program ini dinyalakan, ia akan langsung membombardir PLC dengan angka "0" (`[0]*12`) di laci Override dan mematikan semua saklar `Trip` (False). Ini untuk memastikan HMI dan PLC dalam keadaan "Bersih" tanpa sisa simulasi dari tes Anda sebelumnya.
*   **Baris 148-149:** Mendefinisikan $f_{nom}=50.0$ Hz dan Total Kapasitas Sistem ($S_{Base}$).

## Blok 4: Loop Utama / Jantung Fisika `while True` (Baris 158 - 369)
Ini adalah roda penggerak utama. Diatur agar berputar setiap `DT = 0.1` detik (Baris 161).

### Fase 1: Membaca Status Breaker (Baris 167 - 180)
Membaca Coil dari PLC untuk mengecek apakah `app.py` atau HMI baru saja menekan tombol saklar (Trip/Close). Status ini dimasukkan ke array `gen_online` dan `load_tripped`.

### Fase 2: Hitung Pemakaian Konsumen (Baris 182 - 228)
*   **Baris 185-186:** Mengecek apakah ada perintah "Override/Paksa" dari HMI. Jika Anda menggeser *slider* nilai beban di web, HMI akan menaruh angkanya di laci Modbus `ADDR_OVERRIDE`. 
*   **Baris 199-221:** Proses pengisian nilai beban. 
    *   Jika statusnya `Tripped` (kena pemutusan MILP), maka bebannya langsung **$0$ MW**.
    *   Jika tidak trip, ia menggunakan pola WBP tadi. Agar grafiknya di HMI tidak mulus seperti robot (melainkan bergerigi seperti listrik asli), ditambahkanlah fungsi `noise` (*random.uniform*).
*   **Baris 223:** Menulis hasil akhir beban ini ke sensor PLC `ADDR_LOADS` agar HMI bisa membacanya.

### Fase 3: Fisika Generator (Baris 230 - 274)
Sistem ini menggunakan kecerdasan pembagian beban (seolah-olah ada AGC / *Automatic Generation Control* tingkat dasar). 
*   **Baris 243-249:** Total beban dibagi rata secara proporsional ke semua generator yang hidup.
*   **Baris 256-261:** Rumus **Droop Control** sejati: $dP = -\frac{\Delta f}{f_0 \cdot \text{Droop}} \times P_{rated}$. Jika frekuensi anjlok, target MW generator akan digeber naik.
*   **Baris 263-267:** Limitasi *Ramp Rate*. Generator tidak bisa tiba-tiba melonjak 50 MW dalam sedetik, ada batas kecepatan naiknya. 

### Fase 4: Bencana Frekuensi / *Swing Equation* (Baris 276 - 320)
Ini adalah baris paling fundamental yang membedakan proyek Anda dengan simulasi anak sekolah.
*   **Baris 279 (`delta_p_pu`):** Menghitung Defisit Daya.
*   **Baris 281-282 (`h_eff`):** Menghitung Inersia Total. Jika PLTA (Inersia Berat) mati karena kontingensi, $h_{eff}$ akan anjlok seketika!
*   **Baris 293-294 (`dfdt`):** **The Swing Equation**. Rumusnya: $\frac{df}{dt} = \frac{f_{nom}}{2H} (\Delta P - D)$. Menghitung perlambatan (RoCoF / *Rate of Change of Frequency*) dengan unit Hz/detik.
*   **Baris 298:** Menghitung Frekuensi Baru. $\rightarrow$ `Freq = Freq Lama + (RoCoF * DT)`.
*   **Baris 310-320 (*Blackout*):** Jika semua generator mati, frekuensi meluruh secara eksponensial (kincir turbin berputar semakin pelan karena gesekan hingga berhenti total di 0 Hz).

### Fase Akhir: Penyiaran Data (Baris 322 - 369)
*   **Baris 325-339:** *Engine* selesai menghitung. Saatnya menaruh angka Frekuensi, Jam, Beban Aktual, dan MW Generator ke laci-laci PLC (`client.write_registers`) agar si `app.py` (yang tadinya sedang tidur) bisa terbangun melihat data baru ini.
*   **Baris 345-354:** Mencetak *log* ke terminal hitam Anda agar Anda bisa memantau simulasi tanpa membuka Web.
*   **Baris 364 (`time.sleep(DT)`):** Istirahat sejenak 0.1 detik sebelum menghitung alam semesta kelistrikan ini lagi di siklus berikutnya.
