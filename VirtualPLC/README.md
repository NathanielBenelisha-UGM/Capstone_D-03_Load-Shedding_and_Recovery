# Dokumentasi Virtual PLC (Modbus TCP/IP)

Folder ini berisi rancangan dan konfigurasi Virtual Programmable Logic Controller (PLC) yang disimulasikan menggunakan perangkat lunak **Schneider Electric EcoStruxure Machine Expert - Basic**. PLC ini bertindak sebagai perantara yang mengeksekusi logika *Fail-Safe* fisik dan menyimpan state memori (I/O) yang akan dipolling oleh SCADA HMI.

## 1. Spesifikasi Perangkat Keras (Target Simulasi)
Berdasarkan dokumen `VirtualPLC_Dynamic`, sistem disimulasikan menggunakan arsitektur PLC Schneider:
*   **Controller Utama:** Modicon M221 (`TM221ME16R/G`)
*   **Modul Ekspansi (I/O Bus):**
    *   `TM3DQ8R/G` (Modul Digital Output Relay, 8 Channel)
    *   `TM3AI8/G` (Modul Analog Input, 8 Channel)
    *   `TM3AI8/G` (Modul Analog Input, 8 Channel)
*   **Protokol Komunikasi:** Ethernet (ETH1) dikonfigurasi sebagai **Modbus TCP Server** (Port 502) untuk dapat dihubungkan dengan skrip Python.

![Architecture PLC](VirtualPLC_Dynamic-02.png)

## 2. Peta Memori (Memory Word Mapping)
Integrasi antara mesin fisika (`load.py`), SCADA (`app.py`), dan Virtual PLC berjalan mulus berkat arsitektur pemetaan *Holding Registers* (`%MW`) berikut:

### 2.1. Memori Sensor & Aktuator Utama
*   **%MW0 – %MW11:** Nilai Daya Beban Aktual (MW) untuk setiap titik L101 hingga L405. Ditulis oleh *Physics Engine* secara kontinyu.
*   **%MW30 – %MW33:** Nilai Daya Pembangkitan (MW) untuk setiap Generator (PLTA, PLTS, PLTGU, PLTB). Ditulis oleh *Physics Engine*.

### 2.2. Memori Kontrol SCADA (Coil & Relay Status)
Untuk melakukan pemutusan beban (*Under-Frequency Load Shedding*), SCADA menembakkan sinyal biner (1/0) ke *Internal Memory bits* (kontak) PLC:
*   **%M10 – %M13:** Status fisik generator (1 = Online, 0 = Offline).
*   **%M21 – %M32:** Perintah *Trip* Beban dari algoritma MILP (1 = Putus/Shed, 0 = Normal).
*   **%M50 – %M53:** Perintah *Override / Trip* Generator dari layar HMI SCADA.
*   **%M60 – %M71:** *Feedback Memory*. Menyimpan umpan balik status pembacaan load (*Read Load*) ke SCADA untuk memastikan pemutusan fisik di *breaker* benar-benar terjadi.

### 2.3. Memori Override HMI
*   **%MW40 – %MW51:** Nilai daya paksaan (*Force Override*) dari HMI untuk Beban L101-L405. Jika bernilai `0`, maka *physics engine* menghitung otomatis. Jika `>0`, *physics engine* akan menyesuaikan nilainya sesuai dengan masukan HMI.
*   **%MW52 – %MW55:** Nilai paksaan (*Force Override*) dari HMI untuk output Generator.

## 3. Logika Kontrol Ladder (*Ladder Logic*)
Pemrograman PLC menggunakan *Ladder Diagram* (LD) dengan prinsip keamanan gagal (*fail-safe*):

### 3.1. Rung 0-3 (Kontrol Generator)

![Generator Rungs 1](VirtualPLC_Dynamic-17.png)
*(Gambar: Implementasi Rung 0-2 untuk Generator 1A, 1B, dan 2A)*

![Generator Rungs 2](VirtualPLC_Dynamic-18.png)
*(Gambar: Kelanjutan Rung 3 untuk Generator 2B, memperlihatkan pola fail-safe kontrol)*

```ladder
   %M10 (Status Fisik)      %M50 (Perintah SCADA)     %Q0.0 (Breaker Gen)
---| |-----------------------| / |-----------------------( )---
```
**Penjelasan Skema Generator:**
*   Daya hanya tersalurkan ke jaringan (`%Q0.x` ON) jika sensor fisik menyatakan *Online* (`%M1x` = 1) **DAN** SCADA tidak sedang mengirimkan perintah interupsi (`%M5x` = 0).
*   Jika operator di *Control Room* menekan tombol *Trip*, nilai `%M5x` berubah menjadi `1`, yang seketika membuka kontak Normally Closed (`| / |`) dan mematikan generator dari sistem jaringan secara paksa.

### 3.2. Rung 4-15 (Kontrol Beban / UFLS)

![Load Rungs L101-L201](VirtualPLC_Dynamic-18.png)
*(Gambar: Rung 4-5 yang mengontrol Feeder Prioritas L101 dan L201)*

![Load Rungs L301-L401](VirtualPLC_Dynamic-19.png)
*(Gambar: Rung 6-7 memperlihatkan pemetaan register %M3x dan %M4x ke beban industri)*

![Load Rungs L102-L202](VirtualPLC_Dynamic-20.png)
*(Gambar: Rung 8-9 mengatur pemutusan rangkaian distribusi lapis kedua)*

![Load Rungs L302-L403](VirtualPLC_Dynamic-21.png)
*(Gambar: Rung 10-12 memperlihatkan kelanjutan skema pelepasan untuk klaster beban C dan D)*

![Load Rungs L404-L405](VirtualPLC_Dynamic-22.png)
*(Gambar: Rung 13-15 sebagai penjaga gawang fasilitas terakhir yang diikat pada ujung blok memori %M)*

```ladder
   %M21 (Perintah UFLS MILP)                            %Q0.4 (Suplai Beban L101)
---| / |-----------------------------------------------( )---
```
**Penjelasan Skema Pelepasan Beban (UFLS):**
*   Secara *default*, register UFLS (contoh: `%M21`) bernilai `0`. Karena menggunakan kontak *Normally Closed* (`| / |`), arus logika tetap mengalir dan *coil* penyulang beban (`%Q0.4`) terus menyala.
*   Ketika algoritma *Mixed-Integer Linear Programming* (MILP) di server Python mendeteksi anjloknya frekuensi akibat defisit daya, ia akan menumbalkan beban prioritas rendah dengan menuliskan nilai `1` ke register `%M21`.
*   Secara instan (dalam satuan milidetik), sirkuit PLC terbuka (kontak terputus), *coil* beban mati, dan defisit jaringan terselamatkan. Status nyata matinya *coil* ini dikirim kembali ke SCADA lewat register pembaca `%M60` untuk divisualisasikan pada elemen UI secara *real-time*.

---

## 4. Konfigurasi Sistem (Hardware & Software)

### 4.1. Bill of Material (BoM)
![Bill of Material](VirtualPLC_Dynamic-04.png)
*(Gambar: Daftar komponen perangkat keras yang disimulasikan, mencakup unit utama TM221ME16R/G serta modul ekspansi digital dan analog.)*

### 4.2. Konfigurasi Hardware (I/O Mapping)
![Hardware Config 1](VirtualPLC_Dynamic-05.png)
![Hardware Config 2](VirtualPLC_Dynamic-06.png)
![Hardware Config 3](VirtualPLC_Dynamic-07.png)
![Hardware Config 4](VirtualPLC_Dynamic-08.png)
![Hardware Config 5](VirtualPLC_Dynamic-09.png)
![Hardware Config 6](VirtualPLC_Dynamic-10.png)
*(Gambar: Konfigurasi modul ekspansi I/O pada rak PLC. Memperlihatkan alokasi terminal input/output untuk sensor fisik jaringan dan kontaktor sirkuit beban.)*

### 4.3. Konfigurasi Software (Network & Modbus)
![Software Config 1](VirtualPLC_Dynamic-11.png)
![Software Config 2](VirtualPLC_Dynamic-12.png)
![Software Config 3](VirtualPLC_Dynamic-13.png)
*(Gambar: Pengaturan port Ethernet (ETH1) sebagai Modbus TCP Server. Port 502 dibuka agar server Node.js/Python SCADA dapat melakukan polling data secara berkesinambungan.)*

---

## 5. Manajemen Memori & Simbol

### 5.1. Alokasi dan Konsumsi Memori
![Memory Consumption](VirtualPLC_Dynamic-15.png)
*(Gambar: Ringkasan penggunaan memori internal PLC (%M dan %MW). Menunjukkan bahwa logika Load Shedding ini teroptimasi dan efisien, hanya memakan sebagian kecil dari memori kontroler.)*

### 5.2. Tabel Simbol (Symbol Table)
![Symbol Table 1](VirtualPLC_Dynamic-24.png)
![Symbol Table 2](VirtualPLC_Dynamic-25.png)
*(Gambar: Pemetaan label variabel *human-readable* ke alamat fisik memori PLC. Sangat esensial untuk meminimalisir kesalahan pemetaan memori antara *Backend Python* dengan PLC.)*

### 5.3. Tabel Referensi Silang (Cross-Reference)
![Cross-Reference 1](VirtualPLC_Dynamic-26.png)
![Cross-Reference 2](VirtualPLC_Dynamic-27.png)
*(Gambar: Tabel referensi silang yang melacak di *rung* mana saja sebuah register digunakan (Read/Write). Membantu proses *troubleshooting* skema perlindungan *fail-safe*.)*
