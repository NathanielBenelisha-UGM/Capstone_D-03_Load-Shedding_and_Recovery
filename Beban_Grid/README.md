# Dokumentasi Physics Engine (Dinamika Jaringan)

Folder ini berisi skrip `load.py` yang berfungsi sebagai "Dunia Fisika" dalam simulasi ini. Karena kita menggunakan Virtual PLC (yang notabene tidak terhubung pada jaringan listrik tegangan tinggi asli), skrip ini bertugas memodelkan pergerakan elektron, inersia rotor, beban dinamis, dan frekuensi jaringan secara numerik.

## 1. Pemodelan Beban (*Dynamic Load Profiling*)
Beban di Indonesia jarang statis. Sistem menggunakan model sinusoidal fluktuatif untuk meniru karakter pemakaian daya (*Load Profile*).
*   **Fluktuasi Harian:** Memisahkan profil WBP (Waktu Beban Puncak, sore-malam) dan LWBP (Luar Waktu Beban Puncak, dini hari).
*   **RNG (*Random Number Generator*):** Disuntikkan noise acak mikro-detik agar daya konsumsi (contoh: 21.4 MW) bergejolak persis seperti di dunia nyata.

## 2. Dinamika Jaringan (*Network Dynamics*)

### 2.1. Persamaan Ayunan (*The Swing Equation*)
Ketika sebuah grid stabil, total pasokan daya mekanik dari turbin harus seimbang dengan total konsumsi beban elektrikal. Jika terjadi *Network Deficit* ($\Delta P = P_{Gen} - P_{Load} < 0$), grid akan mulai memakan energi kinetik yang tersimpan pada massa putar seluruh poros turbin generator yang sedang *online*.

Dihitung laju jatuhnya frekuensi (RoCoF / Rate of Change of Frequency) dengan rumus:

$$ \frac{df}{dt} = \frac{f_{nom}}{2 \cdot H_{eff}} \times (\Delta P_{pu} - P_{damping}) $$
*   **$H_{eff}$**: Inersia Efektif Gabungan. PLTA dengan rotor raksasa ($H=5.0$) sangat kuat menahan penurunan, sedangkan PLTS (hanya *virtual inertia* dari *inverter*, $H=0.5$) akan rapuh tanpa bantuan.
*   **$P_{damping}$**: *Load Damping Factor*. Kompensasi mandiri beban; ketika frekuensi drop tegangan turun, motor industri memutar lebih lambat dan mengurangi tarikan energinya.

Metode **Integrasi Euler** (berbasis orde waktu diskrit $\Delta t$) mengeksekusi perhitungan pembaruan frekuensi tiap siklus (100ms):

$$ f_{t+\Delta t} = f_t + \left( \frac{df}{dt} \times \Delta t \right) $$

### 2.2. Governor Droop Control (Kontrol Primer)
Saat frekuensi menurun, sensor pada masing-masing generator merespons dengan membuka katup mekanis secara proporsional terhadap *error*. 

$$ \Delta P = -\left( \frac{f - 50.0}{50.0} \right) \times \frac{1}{R} \times P_{Rated} $$
Di mana $R$ adalah *Droop Setting* (biasanya 4-5%). *Droop control* ini memungkinkan beberapa pembangkit (seperti PLTGU dan PLTA) saling berbagi beban pertolongan secara independen sesuai dengan kapasitas masing-masing generator tanpa perlu komunikasi terpusat.

### 2.3 Automatic Generation Control / AGC (Kontrol Sekunder)
*Governor Droop* menghentikan kejatuhan lebih lanjut, tapi ia akan meninggalkan frekuensi "bertengger" pada angka tunak (*steady state*) seperti 49.8 Hz. Di sini, integrasi AGC (*Integral Controller*) dihidupkan untuk perlahan-lahan (sesuai *Ramp Rate* masing-masing mesin) mendorong frekuensi murni kembali ke batas absolut nominal **50.00 Hz**.

## 3. Komunikasi Dua-Arah dengan Modbus PLC
Setiap iterasi per 0.1 detik, `load.py` tidak hanya menghitung fisika, melainkan juga menembakkan hasilnya melalui protokol Modbus TCP langsung ke ruang Holding Register Virtual PLC. Pada saat yang bersamaan, mesin `load.py` membaca *feedback* `Coil` (apakah sebuah sirkuit telah diputus oleh sinyal *Trip* HMI / UFLS) dan seketika menggugurkan permintaan beban dari generator tersebut pada siklus iterasi fisika berikutnya.
