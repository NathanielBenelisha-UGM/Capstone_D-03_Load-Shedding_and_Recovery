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
```ladder
   %M10 (Status Fisik)      %M50 (Perintah SCADA)     %Q0.0 (Breaker Gen)
---| |-----------------------| / |-----------------------( )---
```
*   Daya hanya tersalurkan ke jaringan (`%Q0.x` ON) jika sensor fisik menyatakan *Online* (`%M1x` = 1) **DAN** SCADA tidak sedang mengirimkan perintah interupsi (`%M5x` = 0).

### 3.2. Rung 4-15 (Kontrol Beban / UFLS)
```ladder
   %M21 (Perintah UFLS MILP)                            %Q0.4 (Suplai Beban L101)
---| / |-----------------------------------------------( )---
```
*   Secara *default*, register `%M2x` bernilai `0`. Karena menggunakan kontak *Normally Closed* (`| / |`), aliran listrik tetap tersambung sehingga *coil* `%Q0.x` menyala.
*   Ketika perhitungan matematis di Python menyatakan bahwa sistem sedang kritis dan menumbalkan beban L101, Python akan mengubah `%M21` menjadi `1`. 
*   Secara instan, sirkuit terbuka (kontak terputus), mematikan *coil* `%Q0.4`, dan beban lepas dari jaringan listrik.
