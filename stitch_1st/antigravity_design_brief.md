# Design Brief: Antigravity Bubble Glass (AETHER_GRID)

## 1. Visi & Kepribadian Brand
**Antigravity** adalah evolusi dari antarmuka industri tradisional. Alih-alih menggunakan tema gelap yang berat, gaya ini mengadopsi estetika yang ringan, futuristik, dan optimis. 
- **Kepribadian:** Klinis, Presisi, Transparan, dan Modern.
- **Filosofi Visual:** "Weightless Oversight" — elemen desain harus terasa seolah-olah mengapung di ruang atmosfer yang bersih.

## 2. Elemen Visual Inti (The Bubble Glass)
Dasar dari gaya ini adalah penggunaan material **Bubble Glass** — permukaan frosted transparan dengan kedalaman 3D yang halus.
- **Surfaces:** Gunakan `backdrop-filter: blur(20px)` dengan opasitas putih antara 40-70%.
- **Borders:** Border putih tipis (1px) dengan opasitas 20% untuk mensimulasikan pantulan cahaya pada tepi kaca.
- **Rounding:** Radius sudut yang sangat besar (32px hingga 48px) untuk menciptakan kesan "kapsul" atau "gelembung" yang organik.
- **Elevation:** Gunakan shadow yang sangat halus dan tersebar luas (`blur: 40px`, `opacity: 5%`) untuk memperkuat efek melayang.

## 3. Palet Warna (Neon on High-Clarity)
Warna digunakan secara strategis untuk fungsionalitas industri tanpa merusak palet minimalis.
- **Background:** Mesh gradient dinamis antara putih bersih (#F8FAFC) dan biru pucat (#E2E8F0).
- **Primary:** Putih Transparan (Glass).
- **Success (Normal):** Electric Neon Green (#00FF41). Digunakan untuk indikator status aktif dengan efek glow/pulsing.
- **Alert (Critical/Defisit):** Glowing Crimson (#FF073A). Digunakan untuk kegagalan kritis atau kondisi defisit.
- **Accent:** Biru Profesional (#1978E5) untuk elemen navigasi aktif.

## 4. Tipografi
Keseimbangan antara keterbacaan manusia dan presisi data teknik.
- **Inter (Sans-Serif):** Digunakan untuk navigasi, label UI, header, dan teks instruksional. Memberikan kesan modern dan bersih.
- **JetBrains Mono (Monospace):** WAJIB digunakan untuk semua data telemetri, angka beban (MW), timestamp, dan log sensor. Ini memisahkan data sistem hidup dari kontrol antarmuka.

## 5. Tata Letak & Spacing (Floating Grid)
- **Margins:** Gunakan margin luar yang besar (min 32px) agar panel tidak pernah menyentuh tepi layar.
- **Gutters:** Jarak antar modul minimal 24px untuk membiarkan background "bernapas" di antara panel kaca.
- **Connections:** Untuk diagram SLD atau Topologi, gunakan garis kurva (cubic-bezier) semi-transparan daripada garis lurus kaku.

## 6. Prinsip Interaksi
- **Lift-on-Hover:** Panel harus memberikan respon visual "terangkat" (scale up 1-2% dan shadow lebih lembut) saat diinteraksi.
- **Bloom Effect:** Saat tombol atau indikator status aktif, cahaya neon harus "berdarah" sedikit ke permukaan kaca di sekitarnya.
- **Data Density:** Meskipun bergaya minimalis, desain harus mampu menampung kepadatan data tinggi khas SCADA dengan menggunakan grid yang terorganisir di dalam kontainer kaca.