# Bedah Kode: `loadflow_module.py` (Simulator Jaringan AC)

File `loadflow_module.py` berfungsi sebagai **Pelukis Peta** sekaligus **Pendeteksi Tegangan**. Saat algoritma MILP bekerja, ia memutus beban secara "buta" hanya dengan mempertimbangkan rumus Aliran Daya DC (hanya peduli arus/MW, tapi tidak peduli tegangan).

Oleh karena itu, modul ini menggunakan pustaka kelistrikan murni bernama `pandapower` untuk menjalankan simulasi **Aliran Daya AC (Newton-Raphson)** secara *real-time* guna melihat efek pemutusan beban tersebut terhadap tegangan ($V$) bus dan beban saluran (*Line Loading*).

Berikut adalah penjelasan fungsi-fungsinya:

## Blok 1: Duplikat Fungsi MILP (Baris 6 - 32)
*   **`solve_milp_shedding(...)`**: Kenapa fungsi ini ada lagi di sini padahal sudah ada di `app.py`? Ini adalah fungsi cadangan/lokal yang dipersiapkan jika sewaktu-waktu modul `loadflow_module` perlu menebak algoritma MILP sendirian tanpa bantuan `app.py`. Isinya 100% sama persis dengan yang ada di `app.py` versi lama.

## Blok 2: Pembangunan Topologi Jaringan `create_network` (Baris 34 - 96)
Fungsi ini membangun cetak biru (*blueprint*) dari *Single Line Diagram* (SLD) yang Anda lihat di layar Web HMI.
*   **Baris 37-46 (Membuat Bus):** Menciptakan terminal-terminal penyambung (*node*). Terdapat tiga jenis level tegangan: `66 kV` untuk area Generator, `150 kV` untuk jalur transmisi utama, dan `20 kV` untuk area distribusi ujung.
*   **Baris 48-56 (Koordinat Geodata):** Menetapkan posisi sumbu $X$ dan $Y$ dari masing-masing bus. Ini berguna agar fungsi plot jaringan mengetahui di mana titik tersebut harus digambar secara visual.
*   **Baris 58-61 (Membuat Trafo):** Menghubungkan bus generator (66kV) ke transmisi utama (150kV). Nilai `sn_mva=125.` artinya trafo ini mampu mengalirkan daya hingga 125 MVA sebelum ia kelebihan beban (*overload*). Trafo juga memiliki rugi-rugi tembaga dan besi yang dimodelkan melalui `vkr_percent` dan `vk_percent`.
*   **Baris 63-68 (Membuat Saluran / Kabel):** `create_line_from_parameters` digunakan untuk membentangkan kabel. `max_i_ka=0.2` (Batas arus maksimal 0.2 kilo-Ampere atau $\approx 52$ MVA). Batas arus ini sengaja disetel rendah agar jika Anda memaksakan generator mengalirkan daya penuh, warna kabel di HMI akan cepat berubah menjadi merah (*Overload > 100%*).
*   **Baris 70-75 (Memasang Generator):** PLTA ditetapkan sebagai *Slack Bus* (`ext_grid`), yaitu terminal referensi tak terbatas yang bertugas menyerap sisa ketidakseimbangan daya aktif (P) maupun reaktif (Q) agar hukum Ohm bisa diselesaikan. Generator lainnya (PLTS, PLTGU, PLTB) dipasang sebagai pembangkit biasa (`sgen`/`gen`).
*   **Baris 77-94 (Memasang Beban):** Menancapkan 12 beban masyarakat (`L101` dkk.) pada bus 150kV dan 20kV.

## Blok 3: Pengekstrak Hasil `get_results_dict` (Baris 98 - 152)
Setelah hukum Ohm dihitung, fungsi ini bertugas menerjemahkan matriks ribuan baris dari `pandapower` menjadi kumpulan *dictionary* sederhana agar mudah dikirimkan ke JavaScript di Web HMI.
*   **Baris 99-101 (`v_profile`):** Mengambil profil tegangan (*Voltage Magnitude / vm_pu*) dari tiap bus. Normalnya bernilai $1.0$ p.u. (per-unit). Jika anjlok di bawah $0.95$ p.u, sistem dalam bahaya.
*   **Baris 103-109 (`t_load`, `l_load`):** Mengambil *loading percent* (persentase beban) dari Trafo dan Saluran transmisi. Jika $> 80\%$, kabel berwarna kuning di HMI. Jika $> 100\%$, kabel berwarna merah.
*   **Baris 116-132:** Menghitung berapa Megawatt daya yang terpaksa dikeluarkan oleh *Slack Bus* untuk menyeimbangkan jaringan.
*   **Baris 142-152:** Membungkus semuanya (*buses, trafos, lines, slack_mw, min_v, max_v*) ke dalam format JSON/Dictionary untuk dikirim pulang ke `app.py`.

## Blok 4: Eksekusi *Real-Time* `run_live_loadflow` (Baris 154 - 247)
Ini adalah fungsi utama yang terus-menerus dipanggil secara asinkron dari latar belakang `app.py`.
*   **Baris 163-190 (Injeksi Data Dinamis - Generator):** Fungsi membaca data generator dari `app.py` (hidup atau mati). Jika generator mati (`in_service = False`), ia mencabut generator itu dari jaringan Pandapower. Jika hidup, dayanya (`p_mw`) disesuaikan dengan keluaran aktual saat ini.
*   **Baris 191-204 (Logika Intertrip):** **Ini Sangat Keren!** Sistem dirancang realistis. Jika kedua generator di sebelah kiri mati (PLTA & PLTS), maka trafo di gardu tersebut (`Trafo 1`) juga akan terputus dari jaringan secara otomatis (Logika Relay Intertrip).
*   **Baris 205-214 (Injeksi Data Dinamis - Beban):** Memeriksa apakah beban tersebut ada di dalam daftar korban pemutusan MILP (`tripped_loads`). Jika ya, matikan saklarnya dari jaringan (`in_service = False`). Jika tidak, paksakan pemakaian dayanya sama persis dengan angka fluktuasi sensor saat ini (`actual_mw`).
*   **Baris 216-232 (*Fallback* Perlindungan):** Pandapower akan *error* (gagal hitung) jika jaringan kehilangan *Slack Bus*. Oleh karena itu, jika PLTA tiba-tiba mati, blok kode ini akan mengangkat derajat generator lain (misal PLTGU) untuk menjadi *Slack Bus* darurat.
*   **Baris 235-241 (Proses Inti - `pp.runpp`):** Menjalankan kalkulasi matriks Jacobian Newton-Raphson. Jika berhasil *converge* (solusi ditemukan), hasilnya diekstrak dengan `get_results_dict()` dan diberi label `success`.
*   **Baris 243-246 (Bencana):** Jika tegangan anjlok terlalu parah hingga matematika Newton-Raphson tidak bisa diselesaikan (*Blackout*), ia akan mengeluarkan status `error: Loadflow Not Converged`. Sinyal ini akan membuat seluruh garis listrik di HMI Anda langsung mati kelap-kelip keabuan.
