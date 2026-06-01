# Dokumentasi Kode Sistem SCADA C251

Dokumen ini memuat penjelasan mendetail mengenai arsitektur kode dan fungsi dari masing-masing *file* Python yang menyusun sistem SCADA Anda. Gunakan dokumen ini sebagai referensi saat ditanya oleh dosen penguji mengenai "bagaimana program Anda bekerja di balik layar".

---

## 1. `SCADA/app.py` (Modul Otak Utama / SCADA Master)
File ini adalah inti dari seluruh sistem SCADA. Ia bertindak sebagai komandan yang memantau PLC, mengambil keputusan (MILP), dan menyajikan data ke layar web (HMI).

*   **Fungsi Utama:**
    *   Berkomunikasi dengan PLC (membaca sensor dan mengirim perintah kontrol) menggunakan protokol Modbus TCP/IP.
    *   Menjalankan server web `Flask` dan `Socket.IO` untuk menampilkan UI HMI ke browser.
    *   Mengeksekusi perhitungan matematika MILP (*Mixed-Integer Linear Programming*) untuk menentukan beban mana yang harus diputus saat darurat.
*   **Komponen & Variabel Penting:**
    *   `solve_milp_shedding(deficit, live_loads, current_tripped, freq_hz)`: Ini adalah *fungsi paling mematikan* di aplikasi Anda. Fungsi ini membangun model optimasi matematika (Syarat: Daya mati $\ge$ Defisit; Tujuan: Minimalkan beban prioritas tinggi yang mati). Ia memanggil *solver* `CBC` dan mengembalikan daftar beban yang harus ditumbalkan (`shed_set`).
    *   `background_monitoring()`: Fungsi *loop* abadi (*while True*) yang berjalan 10 kali per detik. Tugasnya adalah mengecek frekuensi dari PLC. Jika $f \le 49.0$ Hz, ia memicu fungsi MILP. Jika sudah stabil, ia menjalankan prosedur *Recovery* secara sekuensial.
    *   `socketio.emit(...)`: Digunakan untuk melempar data (frekuensi, MW, status saklar) langsung ke *browser* tanpa jeda (*real-time streaming*).

---

## 2. `Beban_Grid/load.py` (Modul Simulator Fisika / *Plant*)
Karena Anda tidak memiliki generator turbin uap atau kincir angin sungguhan di kamar Anda, file ini bertugas menyimulasikan hukum fisika pabrik tersebut secara numerik (*Physics Engine*).

*   **Fungsi Utama:**
    *   Membuat fluktuasi profil beban listrik masyarakat dari jam ke jam agar realistis.
    *   Menghitung **Persamaan Ayunan (*Swing Equation*)** untuk menggerakkan naik-turunnya frekuensi jika terjadi ketidakseimbangan antara *Supply* (Generator) dan *Demand* (Beban).
    *   Bertindak sebagai "Mesin Sensor". Ia selalu mengirim data fisika hasil hitungannya ke memori PLC agar bisa dibaca oleh `app.py`.
*   **Komponen & Variabel Penting:**
    *   `get_load_pct(hour)`: Menghasilkan persentase pemakaian listrik (mirip kurva WBP/LWBP PLN).
    *   *Swing Equation & Droop Control (di dalam loop utama)*:
        Terdapat perhitungan variabel inersia (`H`) dari tiap jenis generator (PLTA berat, PLTS ringan). Jika generator mati (dibaca dari Coil Modbus `%M50-%M53`), daya generator berkurang, *Swing Equation* menghitung defisit daya ($\Delta P$), lalu mengalikan konstanta inersia untuk menurunkan variabel frekuensi (`freq_hz`).
    *   `client.write_registers(ADDR_FREQ, ...)`: Menulis nilai frekuensi yang anjlok tersebut ke laci Modbus `%MW54` di PLC, menunggu `app.py` bereaksi menyelamatkannya.

---

## 3. `SCADA/loadflow_module.py` (Modul Aliran Daya AC & Topologi SLD)
Di awal, MILP bekerja menggunakan hitungan Aliran Daya DC (sangat cepat, tapi tidak bisa menghitung tegangan). Modul ini menggunakan library `pandapower` (simulator AC sungguhan) untuk melengkapi apa yang tidak bisa dilihat MILP.

*   **Fungsi Utama:**
    *   Membuat model jaringan virtual 11-Bus dengan kabel transmisi (*Line*), Trafo, dan percabangan secara akurat berdasarkan hukum Ohm dan Kirchhoff.
    *   Menerima data tebakan MW dari HMI, memasukkannya ke jaringan virtual, dan mengeksekusi perhitungan Newton-Raphson (`pp.runpp`).
    *   Mencari tahu kabel mana yang kelebihan arus (*Overload*) dan bus mana yang tegangannya anjlok ($V < 0.95$ p.u).
*   **Komponen & Variabel Penting:**
    *   `setup_network()`: Membangun peta statis. Di sini Anda mendefinisikan trafo (150kV/20kV) dan saluran (*Line\_1*, *Line\_2*) beserta spesifikasi ketahanan arusnya (`max_i_ka`).
    *   `run_live_loadflow(load_statuses, gen_statuses)`: Fungsi asinkron yang menerima *array* generator mana saja yang hidup, lalu mencocokkannya dengan `pandapower`. 
    *   Output dari file ini (Persentase tegangan dan Persentase beban kabel/ *loading_percent*) akan diserahkan kembali ke Web HMI untuk mengubah warna gambar garis listrik (Single Line Diagram) menjadi Merah (Mati), Kuning (*Warning*), atau Hijau (Aman).

---

## Ringkasan Alur Interaksi (Siklus Berputar):
1. **`load.py`** menghitung fisika bumi $\rightarrow$ melempar hasilnya ke memori **PLC**.
2. **`app.py`** mengambil data dari **PLC** $\rightarrow$ memikirkan mitigasinya $\rightarrow$ menembakkan sinyal putus saklar kembali ke **PLC** $\rightarrow$ dan menyiarkan data tersebut ke layar komputer.
3. Di saat bersamaan, **`loadflow_module.py`** berlari di latar belakang komputer untuk menggambar ulang warna kabel listrik di layar (*Single Line Diagram*) agar terlihat realistis.
