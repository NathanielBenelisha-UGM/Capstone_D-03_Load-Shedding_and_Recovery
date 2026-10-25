# Laporan Uji Skenario — Load Shedding Optimal vs UFLS Statis

Tanggal uji: 25 Okt 2026, Laptop B. Data mentah: `uji/hasil_uji_final.csv`.

## 1. Metode

- **Closed-loop dengan PLC tiruan sebagai proses terpisah.** Server Modbus TCP meniru M241: unit 255, bit %Mn di %MW(10000+n//16), latensi 5 ms per transaksi, dan ladder tiruan `READ_LOAD = NOT trip` (scan 5 ms). `load.py` dan `app.py` memakai kode repo apa adanya. Harness hanya mengganti `socketio.emit` (untuk mencatat log HMI) dan lokasi CSV timing.
- Gangguan diberikan lewat handler HMI yang sama (`gen_control`, `toggle_line`) pada t = 10 s. Kondisi awal: beban puncak 19:00 (±180 MW).
- Fase pemulihan: generator di-ON-kan pada t = 60 s. Untuk skenario feeder, Line_1-2 di-ON-kan pada t = 360 s. Pengamatan 700–800 s.
- Snapshot AC load flow (pandapower) diambil di akhir fase gangguan (sebelum generator ON) dan di akhir run.
- **KPI** (mutlak, pasca-gangguan maupun pasca-pemulihan): nadir f > 49,50 Hz; 0,90 < V ≤ 1,05 pu; loading saluran/trafo ≤ 100%; generator ≤ rating.
- Tiga skema:

| Skema | Isi |
|---|---|
| **optimal_cepat** | Usulan: jalur cepat pre-armed (mask MILP dihitung sebelum gangguan) + supervisor MILP + validasi AC + restorasi bertahap/parsial |
| optimal_supervisor | Sama, tetapi jalur cepat dimatikan (`FAST_TRIP=0`), hanya MILP di loop supervisor |
| ufls_statis | Baseline konvensional (`SCHEME=ufls_static`): 5 tahap 49,5/49,3/49,1/48,9/48,7 Hz, blok tetap L101 / L201 / L303+L304 / L305 / L401, tunda relai 0,1 s. **Setting ilustratif (default), bukan setting acuan utilitas.** Restorasi memakai logika yang sama. |

## 2. Hasil

### 2.1 N-1 PLTA (75 MW, output 67 MW)

| | optimal_cepat | optimal_supervisor | ufls_statis |
|---|---|---|---|
| Nadir gangguan | **49,749 Hz ✅** | 49,478 Hz ❌ | 49,004 Hz ❌ |
| Waktu shedding (trip → feedback PLC) | 131 ms (FAST) | 312 ms | 328 ms (3 tahap relai) |
| Beban dilepas | L303 L305 L401 = 67,4 MW | L303 L305 L401 = 67,9 MW | L101 L201 L303 L304 = 67,2 MW |
| Target / kelebihan | 64,4 MW / +3,0 MW (4,7%) | 65,1 / +2,8 | (tidak ada target) |
| V gangguan / line / trafo | 0,963 pu ✅ / 32,6% / 75,4% ✅ | 0,959 ✅ / 34,1% / 78,5% ✅ | 0,956 ✅ / 47,7% / 78,0% ✅ |
| f selama pemulihan | 49,795–50,208 Hz ✅ | 49,79–50,177 ✅ | 49,81–50,046 ✅ |
| Akhir | semua pulih, Vmin 0,961 ✅ | semua pulih ✅ | semua pulih ✅ |

### 2.2 N-2 PLTA + PLTGU simultan (kehilangan ±135 MW dari 180 MW)

| | optimal_cepat | optimal_supervisor | ufls_statis |
|---|---|---|---|
| Nadir gangguan | 48,884 Hz ❌ | 47,303 Hz ❌ | 45,45 Hz ❌ |
| f tunak pasca-shedding | 50,03 Hz | 49,99 Hz | **48,68 Hz** (tertahan di bawah 49,5 sampai PLTGU ON) |
| Waktu shedding | 150 ms | 231 ms (2 tahap) | 453 ms (5 tahap) |
| Beban dilepas | 8 beban = 136,6 MW (target 134,1, +1,9%) | 136,3 MW | seluruh P2 = 120,1 MW (kurang) |
| V / loading gangguan | 0,987 pu / 17% / 41% ✅ | ✅ | ✅ |
| Setelah PLTGU ON | parsial: L402 L302 L303 L101 pulih, f 49,715–50,57 Hz ✅ | 5 beban pulih, f 49,665–50,40 ✅ | f 48,65–50,18 ❌ |
| Sisa padam akhir | 91,8 MW (target ±65 MW) | 73,5 MW | 67,3 MW |

N-2 PLTA+PLTGU **di luar batas fisik** semua skema: RoCoF awal ±−11 Hz/s, sehingga nadir > 49,5 Hz menuntut shedding < ±46 ms. Waktu itu tidak tercapai lewat Modbus TCP + Wi-Fi. Skema usulan tetap terbaik: nadir 48,88 Hz vs 45,45 Hz pada UFLS statis, frekuensi kembali normal, dan tidak ada underfrequency berkepanjangan.

### 2.3 Line_1-2 + PLTA simultan

| | optimal_cepat | optimal_supervisor | ufls_statis |
|---|---|---|---|
| Nadir gangguan | **49,758 Hz ✅** | 49,489 Hz ❌ | 48,969 Hz ❌ |
| Waktu shedding | 191 ms (FAST) | 324 ms | 444 ms (3 tahap) |
| Beban dilepas | L303 L305 L401 = 66,6 MW (target 64,1, +3,9%) | 67,7 MW | L101 L201 L303 L304 = 68,0 MW |
| Vmin gangguan | 0,923 pu ✅ | 0,921 ✅ | **0,898 pu ❌** |
| Line maks gangguan | 61,7% (Line_1) ✅ | 63,1% ✅ | 80,9% (Line_2) ✅ |
| Pemulihan (PLTA ON → Line ON) | f 49,804–50,065 ✅, semua pulih, Vmin 0,961 ✅ | ✅ | ✅ |

UFLS statis melepas blok tetap (L101/L201 di feeder yang tidak terkendala), sehingga tegangan di sisi Line_1-2 yang terputus turun di bawah 0,90 pu. Skema usulan memilih L305/L401 (bus terdampak) lewat validasi AC.

### 2.4 Rekap KPI

| Skenario | optimal_cepat | optimal_supervisor | ufls_statis |
|---|---|---|---|
| N-1 PLTA | **LOLOS semua** | gagal nadir | gagal nadir |
| N-2 PLTA+PLTGU | gagal nadir (batas fisik); V, loading, pemulihan lolos | gagal nadir | gagal nadir, f tunak, pemulihan |
| Line_1-2 + PLTA | **LOLOS semua** | gagal nadir | gagal nadir + Vmin |

## 3. Analisis latensi dan beban komunikasi

Uji N-1 PLTA dengan PLC tiruan terpisah (server melayani transaksi berurutan seperti M241):

| Kondisi | req/s PLC | Siklus load.py (proses) | Nadir | Total shedding |
|---|---|---|---|---|
| Latensi 5 ms, jalur cepat ON, sebelum mitigasi | 132 | 65 ms | 49,721 | 123 ms |
| Latensi 5 ms, jalur cepat OFF | 103 | 53 ms | 49,481 | 269 ms |
| Latensi 5 ms, ON, diagnosis `[MODBUS]` OFF | 134 | 61 ms | 49,743 | 119 ms |
| **Latensi 5 ms, ON, sesudah mitigasi** | **98** | **36 ms** | 49,759 | 137 ms |
| Latensi 15 ms, ON, sebelum mitigasi | 63 (jenuh) | 221 ms, 494 siklus lambat | 49,417 ❌ | 523 ms |
| **Latensi 15 ms, ON, sesudah mitigasi** | 63 (jenuh) | **122 ms** | **49,674 ✅** | 529 ms |

Temuan:
- **Lonjakan latensi tidak muncul pada PLC tiruan dengan latensi 5 ms.** Load.py stabil 100 ms per siklus (p95 101 ms) dan tidak ada siklus lambat. Macet yang pernah terlihat di simulasi lama adalah artefak harness, karena server tiruan berjalan dalam proses yang sama dengan app (GIL).
- **Yang membuat berat adalah jumlah transaksi berurutan**, bukan perhitungan. Diagnosis `[MODBUS]` tidak terukur pengaruhnya (49,743 vs 49,721 Hz, masih dalam variasi antar-run). Jalur cepat menambah ±30 req/s, tetapi tanpanya nadir gagal (49,48 Hz). Pada latensi 15 ms per transaksi, server jenuh dan semua klien saling mengantre; pola ini sama dengan lonjakan di PLC asli (req/s rendah, latensi tinggi di kedua klien).
- Mitigasi yang dipasang:
  - load.py 8 → 3 transaksi per siklus (FC23 tulis %MW0–33 + baca override; status generator hanya saat berubah);
  - app melewati tulis breaker yang tidak berubah;
  - jalur cepat memakai bacaan loop utama.
- Di PLC asli, sumber utama lonjakan tetap jaringan Wi-Fi range extender. Saran: kabel LAN laptop–TP-Link, matikan hemat daya Wi-Fi, dan pantau dengan `ping -t 192.168.100.200`.

## 4. Perubahan kode selama uji

| Perubahan | Alasan / bukti |
|---|---|
| Tie-break MILP ε·MW (`MILP_EXCESS_EPS = 1e-4`) | Objektif lama hanya menghitung jumlah beban berbobot. Kombinasi 72 MW dan 67,5 MW sama-sama bernilai 3, sehingga solver memilih sembarang. Over-shedding N-1 turun dari +8,7 MW menjadi +3,0 MW; urutan prioritas tidak berubah. |
| Penjaga restorasi (`RESTORE_GUARD_HZ = 49,80`, coba lagi 60 s) | Tanpa penjaga, f turun ke 49,405 Hz saat L305 (27 MW) naik, karena ramp setpoint armada ±0,34 MW/s < 0,54 MW/s. Dengan penjaga: f minimum saat pemulihan 49,79 Hz. |
| Timer restorasi dalam detik nyata | Siklus app ±130 ms (bukan 100 ms), sehingga "3 s" sebelumnya sebenarnya ±4 s. |
| Fallback greedy berprioritas bila MILP tidak Optimal | Sebelumnya: cabang UFLS melepas **semua** beban, cabang defisit tidak melepas apa pun, dan tabel jalur cepat berisi "semua beban". |
| Baseline UFLS statis (`SCHEME=ufls_static`) | Pembanding untuk laporan. |
| load.py 3 transaksi/siklus, FC23 di `plc_modbus.py` | Lihat §3. |
| Reconnect otomatis (`M241Client.execute` menutup socket mati) | Sebelumnya, setelah PLC/Wi-Fi putus, app dan load.py **tidak pernah** tersambung ulang (WinError 10054 terus-menerus). |
| Validasi data PLC (`app.plc_data_valid`) | Sebelumnya, satu pembacaan bit gagal atau PLC yang baru restart (memori 0) terbaca "semua generator OFFLINE", sehingga semua beban dilepas dan f tertahan di 55 Hz (deadlock). Sudah diuji: restart PLC tiruan tidak memicu shedding. |
| HMI: pill PLC dari `feedback.ok` + watchdog 3 s, `interrupt_ack`, override 0,1 MW, hapus kode ganda, skala ×10 `set_gen_interrupt` | Pill lama menampilkan "PLC ONLINE" selama browser tersambung ke Flask walaupun PLC mati. Sudah dicek di browser: ONLINE → "PLC TIDAK ADA DATA" → ONLINE lagi. |

## 4b. Perbaikan model plant: dispatch seimbang + AGC (disetujui Niel, 25 Okt 2026)

Latar belakang (uji PLC asli N-1 PLTGU → ON):
- Saat PLTGU ON, f turun ke 49,81 Hz. Dispatch berbasis bagian kapasitas langsung menurunkan unit lain (PLTS 12,5 MW/s), padahal PLTGU baru sinkron.
- Setelah restorasi L303, f tertahan di ±49,91 Hz karena AGC lama terlalu lemah: 0,05 MW/(Hz·s), batas ±5 MW, dan anti-windup berarah terbalik. Akibatnya restorasi macet.

Perubahan di `load.py`; `GEN_CFG`, inersia, droop, dan respons primer tidak diubah:
- **Dispatch seimbang**:
  - Tahap 1: total setpoint mengikuti beban + bias AGC secepat kemampuan ramp armada.
  - Tahap 2: perataan ke bagian ekonomis (rated/Σrated) tanpa mengubah total. Unit lama hanya melepas beban sebanyak yang diambil unit baru.
  - Runback 3× saat f > 50,05 Hz tetap ada.
- **AGC**: `bias += β·(50 − f)/T·dt`, dengan β = 100 MW/Hz, T = 60 s, dan batas ±20 MW. Integrasi ditahan bila |Δf| > 0,5 Hz atau armada sudah di batas fisik. Nilai ini tipikal untuk sistem kecil/terisolasi dan masih perlu referensi.

Hasil (PLC tiruan, optimal + jalur cepat; v2 = sebelum perbaikan):

| Skenario | Nadir (v2 → baru) | f terendah 30 s setelah ON | f fase pemulihan | Restorasi penuh setelah ON | Pembatalan penjaga |
|---|---|---|---|---|---|
| N-1 PLTGU → ON | – → 49,694 | – → **49,965** | 49,885–50,050 | 287 s | 0 |
| N-1 PLTA → ON | 49,749 → 49,754 | 49,855 → **49,978** | 49,795–50,208 → 49,875–50,050 | 188 → **145 s** | 1 → 0 |
| N-2 → PLTGU ON | 48,884 → 47,874* | 49,715 → **49,977** | 49,715–50,570 → 49,916–50,421 | parsial (sisa 91,8 → 90,0 MW) | 0 |
| Line_1-2 + PLTA → ON → Line ON | 49,758 → 49,758 | 49,848 → **49,986** | 49,804–50,065 → 49,712–50,097 ⚠ | 410 → 391 s | 0 |

- Uji tunak 3 menit tanpa gangguan: rata-rata f per 10 s 49,983–50,016 Hz, tanpa osilasi. Proses load.py tetap 29–34 ms per siklus.
- \*Nadir N-2 ditentukan waktu shedding, bukan oleh perubahan ini. Fisika berjalan per 100 ms, sehingga shedding ≤ ±210 ms masuk di siklus ke-2 (±48,9 Hz) dan > ±230 ms di siklus ke-3 (±47,9 Hz). Tiga run N-2 dengan kode baru: 48,896 Hz (153 ms), 47,905 Hz (236 ms), 47,874 Hz (261 ms). Semuanya tetap di bawah batas fisik 49,5 Hz.
- ⚠ **Bug app.py yang ditemukan dan sudah diperbaiki**: penjaga restorasi (49,80 Hz) tidak aktif untuk beban **terakhir** yang dipulihkan. Setelah L401 dinyalakan, daftar padam kosong dan cabang "SISTEM NORMAL" menghapus status `restoring`, sehingga f turun ke 49,712 Hz saat PLTS sudah di 25 MW. Akar yang sama ada di cabang restorasi parsial (4c) dan UFLS statis (4S): `restoration_step` hanya dipanggil bila surplus ≥ beban padam terkecil, jadi penjaga tidak berjalan begitu surplus mengecil setelah satu beban dinyalakan. Perbaikan: ketiga cabang tetap memanggil `restoration_step` selama `app.restoring` terisi.

Uji ulang setelah perbaikan bug (fix):

| Skenario | Nadir | f terendah 30 s setelah ON | f fase pemulihan | Restorasi penuh | Pembatalan penjaga |
|---|---|---|---|---|---|
| N-1 PLTGU → ON | 49,680 | 49,976 | 49,826–50,054 | 307 s | 0 |
| N-1 PLTA → ON | 49,752 | 49,982 | 49,886–50,065 | 158 s | 0 |
| N-2 → PLTGU ON | 47,840* (236 ms) | 49,978 | 49,904–50,406 | parsial (sisa 90,9 MW, f akhir 50,007) | 0 |
| Line_1-2 + PLTA → ON → Line ON | 49,764 | 49,993 | **49,794**–50,368 | 393 s | 1 (L401, lalu berhasil) |

- Pada Line_1-2 + PLTA, penjaga membatalkan L401 (beban terakhir) di f = 49,79 Hz saat beban sudah 22,5/27,1 MW. L401 dicoba lagi setelah 60 s dan berhasil.
- Lonjakan ke 50,368 Hz terjadi sesaat setelah pembatalan, karena 22,5 MW lepas sekaligus. Masih di bawah 50,5 Hz dan kembali normal lewat governor dan AGC.
- Semua run tanpa peringatan `[DT]`/`[IO]`, dan Vmin akhir 0,96 pu.

### Kriteria cadangan restorasi yang konsisten dengan penjaga (cara A, disetujui Niel)

Penyebab pembatalan L401 (Line_1-2 + PLTA):
- Saat L401 dinyalakan, PLTS (BESS) sudah di 25 MW (mentok). Cadangan cepat 30,8 MW, sedikit di atas syarat 30,1 MW, seluruhnya berasal dari ramp unit lambat ditambah respons primer **penuh**.
- PLTA baru mengeluarkan respons primer penuh (7,5 MW) saat f turun ±0,25 Hz, yaitu di bawah penjaga 49,80 Hz. Akibatnya f merosot ke 49,79 Hz dan L401 dibatalkan.

Perubahan di `app.py` (`restore_headroom`):
- Respons primer yang dihitung = min(pfr, rated × Δf / (droop × 50)), dengan Δf = `RESTORE_DF_HZ` = 0,15 Hz. f boleh turun sampai ±49,85 Hz, dan sisa 0,05 Hz di atas penjaga untuk noise dan lag. Hasilnya: PLTA 7,5 → 4,5 MW, sedangkan PLTGU, PLTB, dan PLTS tidak berubah (PLTS dihitung dari ruang kosongnya karena ramp-nya cepat).
- Tanpa suku damping beban, sehingga kriteria selalu sama atau lebih ketat dari rumus lama.
- `droop` ditambahkan ke `GEN_MAP` (harus sama dengan `GEN_CFG` load.py). `RESTORE_MARGIN_MW` tetap 3 MW: pelonggaran ke 1,5 MW dibatalkan sebelum diuji.

Hasil PLC tiruan (sebelum = "fix", sesudah = cara A):

| Skenario | f terendah fase pemulihan | f tertinggi | Restorasi penuh | Pembatalan penjaga |
|---|---|---|---|---|
| N-1 PLTGU → ON | 49,826 → **49,882** | 50,054 → 50,056 | 307 → 265 s | 0 → 0 |
| N-1 PLTA → ON | 49,886 → 49,857 | 50,065 → 50,040 | 158 → 192 s | 0 → 0 |
| Line_1-2 + PLTA → ON → Line ON | 49,794 → **49,878** | 50,368 → **50,065** | 393 → 426 s | **1 → 0** |

- Pembatalan L401 hilang. Tidak ada lagi padam kedua, dan lonjakan 50,37 Hz akibat pembatalan juga hilang.
- f selama restorasi di semua skenario ≥ 49,857 Hz, sesuai target rancangan (≥ ±49,85 Hz).
- Restorasi bisa sedikit lebih lama (±+33 s pada N-1 PLTA dan Line_1-2 + PLTA). Selisih pada N-1 PLTGU (lebih cepat) masih dalam variasi antar-run.
- Nadir saat trip tidak berubah (49,70–49,76 Hz). Ayunan f saat restorasi tetap terlihat (49,86–50,06 Hz).

**Perubahan parameter sesudah uji di atas (keputusan Niel, 25 Okt 2026):** `RESTORE_MARGIN_MW` 3 → **0 MW** dan penjaga `RESTORE_GUARD_HZ` 49,80 → **49,60 Hz**, yang masih di atas tahap 1 UFLS konvensional 49,50 Hz. Tabel di atas memakai 3 MW / 49,80 Hz.

Perkiraan dampak:
- Beban besar bisa menyala ±45 s lebih awal saat cadangan pas-pasan.
- f saat beban naik bisa turun ke ±49,77–49,80 Hz, karena tanpa margin, noise dan rugi-rugi tidak lagi tertutup. Ini di luar rentang normal 50 ± 0,2 Hz, tetapi di atas penjaga baru.
- Jarak penjaga ke batas KPI 49,50 Hz menyempit dari 0,30 menjadi 0,10 Hz.
- Hasil terukur menunggu uji PLC asli.

### Uji PLC asli setelah perbaikan: N-1 PLTGU → ON (25 Okt 2026, log `uji/log_plc/`)

| Ukuran | Sebelum perbaikan (PLC asli) | Sesudah (PLC asli) |
|---|---|---|
| Nadir trip | 49,695 Hz | **49,697 Hz** ✅ (FAST, total 113 ms) |
| f terendah 30 s setelah ON | 49,81 Hz | **49,984 Hz** |
| f selama pemulihan | tertahan ±49,91 Hz, restorasi macet setelah L303 | 49,911–50,042 Hz ✅, kembali ke 50,00 Hz ±110 s setelah ON |
| Beban pulih (pengamatan 255 s) | L303 | L303, L305 |

- Saat PLTGU sinkron, PLTS (BESS) melepas beban seiring PLTGU mengambilnya (22,4 → 3,3 MW), sehingga total generasi tetap sama dengan beban.
- L401 masih menunggu cadangan cepat. Pada akhir pengamatan (+255 s) cadangan cepat 25,4 MW, padahal L401 butuh 27,1 + 3 MW. Cadangan naik ±0,07 MW/s, mengikuti ramp PLTGU 0,08 MW/s yang membuat PLTA melepas beban, jadi diperkirakan L401 menyala sekitar +315 s dan restorasi penuh sekitar 6,5 menit setelah ON. Run dihentikan sebelum itu.
- Latensi: satu episode `[MODBUS] LAMBAT` (maks 185 ms, req/s turun 43 → 18), dan load.py sekali 353 ms (`[DT]`, fisika dibatasi 300 ms) saat L305 naik. Ping selama uji rata-rata 1,8 ms, maks 31 ms, tanpa timeout. Ping hanya 1×/detik, sehingga jeda sesaat bisa terlewat; jam simulasi load.py juga tidak bisa dicocokkan persis dengan jam ping. Pola "kedua klien lambat + req/s turun" tetap sama dengan temuan sebelumnya.

### Uji PLC asli panjang dengan margin 0 MW + penjaga 49,60 Hz (25 Okt 2026, 38 menit, log `uji/log_plc/`)

| Waktu sim | Kejadian | Nadir / f terendah | f tertinggi | f < 49,80 | Catatan |
|---|---|---|---|---|---|
| 19:01 | N-1 PLTGU | **49,694** ✅ | 50,071 | 0,3 s | FAST 129 ms. ⚠ Supervisor menambah L303 (lihat temuan 1) |
| 19:01–19:06 | PLTGU ON → restorasi | 49,912 | 50,078 | 0 s | L303, L101, L304, L305 pulih |
| 19:06 | N-1 PLTA (saat L305 masih naik) | **49,631** ✅ | 49,966 | 4,5 s | FAST 41 ms |
| 19:06 | + PLTGU (38 s kemudian, N-2 beruntun) | **49,548** ✅ | 50,148 | 0,5 s | FAST 136 ms; beban tinggal ±45 MW |
| 19:07–19:15 | PLTGU ON, PLTA ON → restorasi | 49,712 | **52,157** ⚠ | 6,0 s | Lonjakan frekuensi (lihat temuan 2), lalu 7 beban pulih |
| 19:16 | N-1 PLTA | **49,716** ✅ | 50,059 | 1,0 s | FAST 167 ms |
| 19:17–19:21 | PLTA ON → restorasi | 49,840 | 50,052 | 0 s | 4 beban pulih |
| 19:21 | N-1 PLTGU | **49,732** ✅ | 50,104 | 0,6 s | FAST 75 ms |
| 19:22–19:31 | PLTGU ON → restorasi (+ Line_1-1 putus/sambung) | 49,863 | 50,277 | 0 s | Beban dilepas lagi oleh logika feeder lalu pulih |
| 19:31 | PLTA + PLTS (1,3 s, N-2 beruntun) | **49,724** ✅ | 50,422 | 1,3 s | FAST tahap 1 + 2 |
| 19:32–19:38 | PLTS ON, PLTA ON → restorasi | 49,727 | 50,130 | **20,4 s** | Margin 0: penurunan lebih panjang |

- **KPI nadir > 49,50 Hz terpenuhi di semua 6 gangguan** (terendah 49,548 Hz pada N-2 beruntun). Penjaga 49,60 tidak pernah terpicu, dan tidak ada pembatalan.
- **Efek margin 0 / penjaga 49,60**: f terendah saat restorasi 49,712–49,727 Hz, dan f berada di bawah 49,80 Hz selama 6–20 s per fase pemulihan. Sebagai pembanding, dengan margin 3 MW / penjaga 49,80 di PLC asli f terendahnya 49,911 Hz dan tidak pernah di bawah 49,80.
- **Jaringan**: ping rata-rata 2,4 ms, tetapi tercatat lonjakan sampai 243 ms dan 1 *timeout*. Bersamaan dengan itu muncul beberapa `[MODBUS] LAMBAT` (163–333 ms). Ini memperkuat kesimpulan bahwa lonjakan latensi berasal dari Wi-Fi.

**Temuan 1 — over-shedding setelah jalur cepat (19:01).**
- Jalur cepat melepas L101 + L304 + L305 = 64,4 MW, sedangkan target supervisor saat trip 64,5 MW (selisih 0,1 MW akibat beban bergeser sejak tabel dihitung).
- Generator yang baru hilang menembus dead-band, sehingga MILP supervisor menambah satu beban utuh (L303, 13,8 MW). Total 78,2 MW, atau over-shedding ±14 MW.

**Temuan 2 — over-frekuensi 52,16 Hz setelah N-2 (19:07).**
- Beban tinggal ±48 MW. Saat PLTGU dan PLTA di-ON-kan, keduanya sinkron ke output minimum teknis (30 + 15 MW). Bersama PLTB (±17 MW, turun hanya 0,06 MW/s), generasi menjadi ±62 MW.
- Unit di minimum tidak bisa turun lagi, sehingga f naik ke 52,16 Hz.
- Restorasi beban, yang justru akan menurunkan f, ditahan karena f di luar jendela 49,95–50,25 Hz. Restorasi baru jalan setelah PLTB turun cukup jauh.
- Di sistem nyata, 52 Hz dapat memicu proteksi frekuensi lebih generator.

**Perbaikan atas dua temuan di atas (disetujui Niel; belum diuji di PLC asli):**
1. **Toleransi setelah jalur cepat** (`SHED_TOL_MW = 1`). Bila generator baru hilang tetapi beban yang sudah dilepas ≥ target − 1 MW, supervisor tidak menambah beban. Toleransi 1 MW sama dengan yang sudah dipakai kondisi cabang 4c. Kekurangan > 1 MW tetap diproses MILP, dan UFLS bertunda tetap menjadi pengaman. Kasus 19:01 seharusnya menjadi 64,4 MW, bukan 78,2 MW.
2. **Restorasi korektif saat over-frekuensi.**
   - Bila f > 50,25 Hz dan masih ada beban padam, beban ≤ surplus generasi terukur (Σ output generator − Σ beban, dari PLC) boleh dinyalakan tanpa menunggu f stabil. Cabang f > 51,5 Hz ikut memanggilnya.
   - Syarat lain tetap: satu beban per langkah, `keep_mw` pada restorasi parsial, validasi AC, dan penjaga.
   - Ditambah **peringatan sinkronisasi** di HMI (tidak memblokir) bila generasi minimum yang tak terhindarkan selama ±15 s sinkronisasi melebihi beban.
   - Uji logika terisolasi: kasus 19:07 (f 52,16 Hz, surplus 12,5 MW) → L302 (9 MW) dinyalakan, dan peringatan muncul ("59 MW > 48 MW"). Pada N-1 PLTGU ON biasa (beban 112 MW) peringatan tidak muncul.
   - Catatan jujur: karena beban naik pelan (soft-start 2%/s), restorasi korektif terutama **mempersingkat** masa over-frekuensi; puncak f setelah sinkronisasi belum tentu turun banyak.

### Uji PLC asli setelah perbaikan 1 + 2 (25 Okt 2026 malam, 42 menit, margin 0 / penjaga 49,60)

| Kejadian | Nadir / f terendah | Catatan |
|---|---|---|
| N-1 PLTA (5×) | 49,750–49,815 ✅ | FAST 67–211 ms. Perbaikan 1 bekerja: tiap trip `... -> tidak ditambah`, termasuk kasus kurang **+0,4 MW** yang dulu akan menambah satu beban utuh |
| N-2 PLTA → PLTGU (selang 1,6 s) | **49,462 ❌** | f jatuh 0,525 Hz dalam **satu** siklus fisika (RoCoF −5,2 Hz/s, sisa inersia hanya PLTS + PLTB). Beban sudah lepas di siklus berikutnya, jadi ini batas fisik, bukan kelambatan SCADA |
| PLTA + PLTS (selang ±2 s) | 49,815 ✅ | FAST tahap 2 |
| N-1 PLTS | 49,949 ✅ | FAST 147 ms |
| Line_1-1 / Line_1-2 putus-sambung | – | ditangani logika feeder |
| Fase pemulihan (6×) | 49,759–49,908; satu kali **49,575** ⚠ | lihat di bawah |

- **Peringatan sinkronisasi** muncul 3× (50 > 45 MW, 59 > 56 MW, 101 > 92 MW). **Restorasi korektif tidak terpicu**: over-frekuensi kali ini ringan (maksimum 50,46 dan 50,52 Hz, ±12–13 s di atas 50,25), karena beban P3 sudah sebagian pulih sebelum PLTA ON dan surplus terukur lebih kecil dari beban padam terkecil. Belum ada kasus seperti 52 Hz untuk membuktikannya di PLC asli; logikanya baru terbukti lewat uji terisolasi.
- **Penurunan 49,575 Hz** (f < 49,60 selama 4,1 s) terjadi saat L401 (beban terakhir) naik. Penyebabnya:
  - **margin 0**: cadangan 27,8 MW untuk L401 26,5 MW, sementara PLTS sudah 25 MW (mentok);
  - **penjaga berhenti mengawasi saat beban mencapai 90%**, padahal kekurangan generasi terbesar terjadi di ujung kenaikan.
  KPI tetap > 49,50, tetapi sisa jaraknya hanya 0,075 Hz.
- **Jaringan sesi ini paling buruk**: ping 11× *timeout*, 18× > 100 ms (maks 334 ms); load.py 168× `[DT]` (siklus maks 6,8 s, 17× > 1 s); app 226× `[MODBUS] LAMBAT` (maks 1,4 s). Saat siklus > 300 ms, fisika dibatasi 300 ms, sehingga waktu simulasi hilang dan dinamika di periode itu kurang akurat.
- Kosmetik: baris `[TARGET SHED] ... tidak ditambah` juga tercetak saat generator di-ON-kan, karena perubahan daftar generator online dihitung sebagai "trip". Tidak memengaruhi keputusan.

## 5. Batasan dan catatan jujur

- **Penyederhanaan penyalaan beban (disengaja, disetujui Niel).** Beban yang dipulihkan dimodelkan naik bertahap 2%/s dari nominalnya (soft-start ±50 s, maksimum ±1,1 MW per 100 ms). Di sistem nyata, menutup breaker feeder 13–27 MW pada sistem ±150 MW menimbulkan lonjakan seketika 10–18%, sering lebih besar karena *cold load pickup* (1,5–2× selama belasan detik). Lonjakan itu menghasilkan RoCoF sesaat ±0,5–1 Hz/s dan penurunan f ±0,1–0,3 Hz dalam beberapa detik pertama.
  - Akibatnya, ayunan f saat restorasi di uji ini lebih halus daripada kenyataan. Di PLC asli tercatat RoCoF ≤ 0,04 Hz/s; penurunan terdalam 49,911 Hz saat L305 naik, akibat generator yang lambat mengikuti.
  - Penjaga 49,80 Hz dan kriteria cadangan cepat (berbasis kenaikan 50 s) belum diuji terhadap lonjakan seketika.
  - Penyederhanaan ini berlaku sama untuk semua skema dan tidak memengaruhi fase shedding atau nadir, sehingga perbandingan skema tetap adil. Dinamika generator (inersia, governor, ramp, AGC) tetap dimodelkan penuh.

- PLC tiruan bukan M241 asli. Waktu deteksi di PLC asli bergantung pada Wi-Fi; run PLC asli sebelumnya: N-1 PLTGU 49,685 Hz, ±190 ms.
- Satu run per kombinasi. Variasi antar-run nadir N-1 ±0,02 Hz (jalur cepat) dan lebih besar pada supervisor/statis (mis. statis N-1 48,76 vs 49,00 Hz pada run v1/v2).
- Setting UFLS statis ilustratif. Hasil pembanding harus diulang bila setting acuan (PLN/IEEE) tersedia.
- Feedback PLC berasal dari logika ladder, bukan relay fisik.
- Rumus inersia agregat (temuan E) belum diubah: RoCoF kode 3,05 Hz/s vs rumus 4,86 Hz/s setelah PLTGU trip. Ini bisa menggeser nadir. Temuan F (AGC) sudah diperbaiki di §4b; tabel §2 masih hasil sebelum perbaikan (v2) dan baru diulang untuk skema optimal + jalur cepat.
- Restorasi parsial N-2 masih menyisakan 91,8 MW padam (target ±65 MW) di akhir 700 s, karena ramp PLTGU 0,08 MW/s dan penjaga membatalkan L201 sekali. Restorasi terukur dan aman, tetapi lambat.
