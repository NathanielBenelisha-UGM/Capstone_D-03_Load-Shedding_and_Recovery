# ⚡ SCADA Grid Simulator & Dynamic Load Shedding: Comprehensive Technical Documentation

Proyek ini adalah simulasi *Real-Time Digital Twin* berskala industri untuk Sistem Tenaga Listrik, yang mensimulasikan dinamika frekuensi transien, *Governor Droop Control*, hingga optimasi pelepasan beban (*Under-Frequency Load Shedding / UFLS*) menggunakan algoritma *Mixed-Integer Linear Programming* (MILP). Sistem berjalan pada resolusi sangat tinggi (100 ms / 10 FPS) untuk memodelkan kelakuan kelistrikan asli secara *real-time*.

---

## 🏛️ 1. Arsitektur Komunikasi Sistem (System Schema)

Keseluruhan ekosistem terdistribusi ke dalam tiga layer utama yang direkatkan oleh protokol komunikasi **Modbus TCP/IP**.

```mermaid
graph TD
    subgraph "Layer 1: Physics Engine (load.py)"
        B[Beban Grid Dinamis WBP/LWBP]
        G[Generator Dynamics & Swing Equation]
    end

    subgraph "Layer 2: Modbus PLC (Virtual)"
        M[(Holding Registers & Coils)]
    end

    subgraph "Layer 3: SCADA Master & UI (app.py)"
        S[SCADA Server & MILP Solver]
        W[Web HMI - Dashboard]
    end

    B -->|Tulis Beban Aktual| M
    G -->|Tulis Output Daya & Frekuensi| M
    M -.->|Baca Target Beban & Perintah Trip| B
    M -.->|Baca Status Breaker| G

    M ==>|Polling 10 FPS| S
    S ==>|Write Trip Commands (UFLS)| M
    
    S -->|Socket.io Telemetry (10 FPS)| W
    W -->|Control Override & Breaker Trip| S
```

---

## ⚙️ 2. Physics Engine: Simulasi Kinetika Frekuensi (`load.py`)

Bagian ini bukan sekadar animasi rasio, melainkan mesin fisika *differential equations* yang memodelkan putaran rotor sinkron di dunia nyata.

### A. Persamaan Ayunan (The Swing Equation)
Akar dari stabilitas grid adalah kekekalan energi kinetik turbin. Jika terdapat *Network Deficit* (Beban > Generator), energi kinetik turbin akan tersedot untuk menutupi defisit, menyebabkan putaran rotor (frekuensi) melambat.

Secara matematis, sistem menghitung **RoCoF** (*Rate of Change of Frequency* atau $df/dt$) menggunakan modifikasi *Swing Equation*:

$$ \frac{df}{dt} = \frac{f_{nom}}{2 \cdot H_{eff}} \times (\Delta P_{pu} - P_{damping}) $$

**Penjelasan Variabel:**
*   **$f_{nom}$**: Frekuensi nominal sistem (50.0 Hz).
*   **$\Delta P_{pu}$**: *Network Deficit* (Total Generator - Total Beban) dibagi daya dasar (S_Base = 200 MVA). Jika generator kurang, nilai ini negatif.
*   **$H_{eff}$** (*Effective Inertia*): Total momentum inersia jaringan. Dihitung dengan mengalikan konstanta inersia tiap generator (PLTA H=5, PLTGU H=3, PLTS/PLTB H=0.5) dengan proporsi outputnya. Semakin besar $H_{eff}$, semakin tahan grid terhadap *shock* jatuhnya frekuensi.
*   **$P_{damping}$**: *Load Damping Factor*. Di dunia nyata, saat frekuensi turun, motor induksi di pabrik akan melambat sehingga menyerap daya lebih sedikit. Ini dimodelkan sebagai $D \times \frac{(f - f_{nom})}{f_{nom}}$ yang otomatis membantu mengerem laju jatuhnya frekuensi.

Setelah RoCoF didapatkan, frekuensi untuk frame (detik) berikutnya didapatkan melalui integrasi Euler:
$$ f_{new} = f_{old} + \left( \frac{df}{dt} \times \Delta t \right) $$

### B. Primary Control: Governor Droop
Saat frekuensi turun, katup uap/air pada turbin secara otomatis membuka lebih lebar untuk menambah daya. Ini disebut *Droop Control*:
$$ \Delta P_{target} = -\left( \frac{f - 50.0}{50.0} \right) \times \frac{1}{\text{Droop}} \times P_{Rated} $$

### C. Secondary Control: Automatic Generation Control (AGC)
Droop control akan menahan frekuensi agar tidak terus jatuh, tetapi ia akan tertahan/stabil di angka di bawah 50 Hz (misal 49.8 Hz). AGC bertugas menggeser target pelan-pelan untuk mengembalikan frekuensi tepat ke **50.0 Hz** dengan membaca sisa cadangan berputar (*Spinning Reserve*).

### D. Generator Trip (Transient Drop)
Jika user mematikan generator dari HMI, output generator tersebut akan **instan di-set ke 0 MW** (mengabaikan *Ramp-Rate* normal). Hal ini memicu *Network Deficit* instan yang sangat besar, membanting nilai $\Delta P_{pu}$ ke dasar laut, dan memicu *deep-dip* frekuensi yang curam.

---

## 🧠 3. SCADA Master & MILP Load Shedding (`app.py`)

Ini adalah sistem cerdas yang melindungi Grid dari *Blackout* (Mati Listrik Total). Jika frekuensi dibiarkan jatuh, generator akan rusak. Karenanya, sistem harus memutus sebagian rumah/pabrik (Shedding).

### A. Deteksi Defisit (Trigger UFLS)
Algoritma di `app.py` secara konstan mengawasi `capacity_deficit` dan `freq_hz`.
Jika frekuensi menyentuh ambang kritis **$\le 49.50$ Hz**, SCADA akan membunyikan alarm *Under-Frequency Load Shedding (UFLS)*.

### B. Mixed-Integer Linear Programming (MILP)
Alih-alih memadamkan beban secara acak, SCADA menggunakan kecerdasan buatan matematis (Library `PuLP`) untuk mencari kombinasi pemadaman **paling optimal**. 

**1. Decision Variables (Variabel Keputusan):**
$x_i \in \{0, 1\}$
Beban $i$ akan dipertahankan ($x_i=1$) atau dipadamkan ($x_i=0$).

**2. Objective Function (Fungsi Tujuan):**
Tujuannya adalah *meminimalisir total dampak pemadaman*, yang dihitung dari total daya beban dikalikan Bobot Penalti (Priority/Penalty). Beban VIP (seperti Rumah Sakit) memiliki penalti tinggi sehingga sistem akan sebisa mungkin menghindari memutusnya.
$$ \text{Minimize} \sum_{i=1}^{N} (1 - x_i) \cdot P_i \cdot W_i $$
*Di mana $P_i$ adalah daya beban $i$, dan $W_i$ adalah bobot kepentingannya.*

**3. Constraints (Kendala Matematis):**
Jumlah daya dari beban yang **dipadamkan** harus lebih besar atau setara dengan defisit yang dialami jaringan, agar sistem kembali seimbang:
$$ \sum_{i=1}^{N} (1 - x_i) \cdot P_i \ge P_{Defisit} $$

Sistem akan memproses matriks ini dalam hitungan milidetik dan mengeksekusi penulisan register Modbus *Trip* seketika itu juga.

### C. Live N-1 Contingency Matrix
Bahkan saat grid normal (50 Hz), MILP bekerja tanpa henti di belakang layar untuk **memprediksi masa depan**. Ia menghitung 4 skenario N-1 secara berulang (Bagaimana jika PLTA mati? Bagaimana jika PLTGU mati?). Hasil prediksi ini dikirim ke UI berupa *Preselection Matrix*, di mana titik-titik blok **merah** menunjukkan *Feeder* mana yang akan otomatis terpotong **JIKA** suatu saat generator tersebut mati.

---

## 🖥️ 4. HMI & Telemetry Interface (`main.js` & `index.html`)

HMI web dibangun bukan hanya sebagai display, melainkan stasiun *Control Room* yang *responsive*.
*   **10 FPS Polling Rate:** Backend menembakkan event `grid_update` via **Socket.IO** setiap 100 milidetik.
*   **Transient Plotting:** Komponen `Chart.js` menampung memori melingkar sebanyak 120 titik (Array(120)). Dengan suplai data 10 FPS, grafik menampilkan pergerakan transient beresolusi tinggi selama **12 detik ke belakang**, memperlihatkan kedalaman lengkungan (*Nadir*) frekuensi saat terjadi *shock* beban.
*   **Memory Auto-Reset:** Setiap kali program di-restart, HMI memaksa PLC untuk menghapus memori *Load Trip* dan *Manual Override* lama, sehingga grid tidak pernah "terkunci" (*latched*) oleh kondisi *Blackout* masa lalu.

---

## 🚀 5. Cara Penggunaan & Deployment

Sistem dirancang *multi-threaded* dan terdistribusi.

1. **Jalankan Aplikasi PLC:**
   Pastikan OpenModScan / Modbus PLC simulator Anda berjalan.
2. **Terminal 1: Jalankan Engine Fisika**
   ```bash
   cd Beban_Grid
   python load.py
   ```
   *Note: Ini akan menampilkan log per-detik pergerakan frekuensi dan RoCoF.*
3. **Terminal 2: Jalankan SCADA Master**
   ```bash
   cd Scada_22-5-26
   python app.py
   ```
4. **Buka Control Room (Browser)**
   Arahkan browser ke `http://127.0.0.1:5000/?role=admin`.
   
   *Silakan coba tekan tombol "TRIP" pada kotak PLTGU, dan saksikan pertempuran fisika dan matematika MILP menyeimbangkan Grid secara Real-Time pada Transient Plot!*
