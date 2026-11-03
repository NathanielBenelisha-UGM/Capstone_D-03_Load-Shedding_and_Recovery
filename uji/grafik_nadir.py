"""Grafik "Nadir Frekuensi per Skenario Gangguan" untuk banner/laporan.

Cara pakai:
  1. Ubah daftar SKENARIO di bawah (tambah/hapus baris, ganti angka).
  2. Jalankan:  python uji/grafik_nadir.py
  3. Hasil: uji/grafik_nadir.svg (vektor, untuk Canva) dan uji/grafik_nadir.png (300 dpi).

Satu nilai -> titik; rentang (min, maks) -> batang dengan dua titik.
Nilai di bawah SUMBU_MIN digambar sebagai panah merah di tepi kiri.
"""
import os

import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

# ------------------------------- DATA (ubah di sini) -------------------------------
# (nama skenario, nadir terendah [Hz], nadir tertinggi [Hz]); satu kali uji -> isi angka yang sama dua kali
SKENARIO = [   # sumber: log uji PLC asli 26, 27, 29 Okt 2026 + tabel uji tim
    ('N-1 PLTGU (75 MW)', 49.67, 49.75),
    ('N-1 PLTA (75 MW)', 49.53, 49.82),
    ('N-1 PLTB (25 MW)', 49.89, 49.91),
    ('N-1 PLTS (25 MW)', 49.95, 49.95),
    ('Line_1-2 putus → PLTA trip', 49.79, 49.79),
    ('Line_1-1 putus → PLTGU trip', 49.85, 49.85),
    ('N-2 PLTA → PLTB (selang 0,1 s)', 49.70, 49.70),
    ('N-2 PLTB → PLTGU (selang 1,3 s)', 49.70, 49.70),
    ('N-2 PLTA → PLTS (selang ±2 s)', 49.72, 49.82),
    ('N-2 serentak PLTGU + PLTS', 49.62, 49.66),
    ('N-2 serentak PLTA + PLTGU', 49.37, 49.53),
]
KPI_HZ = 49.25              # garis batas KPI
SUMBU_MIN, SUMBU_MAX = 49.2, 50.0
JUDUL = ''                  # mis. 'Nadir Frekuensi per Skenario Gangguan'; kosong = tanpa judul (judul ditulis di Canva)
# -----------------------------------------------------------------------------------

NAVY, NAVY2, BAR, RED, INK, MUTED, GRID, ZONE = '#0B2E5E', '#1F4E9A', '#A9BEDF', '#C62828', '#1A2433', '#6B7787', '#D9DFE8', '#FBE9E9'
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'grafik_nadir')
koma = lambda v, n=2: f'{v:.{n}f}'.replace('.', ',')

_ada = {f.name for f in font_manager.fontManager.ttflist}
FONT = next(f for f in ('Arimo', 'Arial', 'DejaVu Sans') if f in _ada)     # Arimo bila terpasang (sama dgn banner)
plt.rcParams.update({'font.family': FONT, 'svg.fonttype': 'none'})       # teks SVG tetap teks (bisa diedit)
n = len(SKENARIO)
fig, ax = plt.subplots(figsize=(10.6, 0.66 * n + (1.3 if JUDUL else 0.9)))
ys = list(range(n))[::-1]                                  # baris pertama di atas

ax.axvspan(SUMBU_MIN, KPI_HZ, color=ZONE, zorder=0)
ax.axvline(KPI_HZ, color=RED, lw=2, ls=(0, (6, 4)), zorder=1)
for y, (nama, lo, hi) in zip(ys, SKENARIO):
    if hi < SUMBU_MIN:                                     # di luar skala -> panah merah
        ax.plot(SUMBU_MIN + 0.012, y, marker='<', ms=13, color=RED, clip_on=False, zorder=3)
        teks, warna = f'{koma(hi)} < KPI', RED
    else:
        if hi > lo:
            ax.plot([lo, hi], [y, y], color=BAR, lw=9, solid_capstyle='round', zorder=2)
        ax.scatter(sorted({lo, hi}), [y] * len({lo, hi}), s=170, color=NAVY2, edgecolor='white', linewidth=2, zorder=3)
        teks = f'{koma(lo)}–{koma(hi)}' if hi > lo else koma(lo)
        warna = RED if lo < KPI_HZ else INK
    ax.text(1.03, y, teks, transform=ax.get_yaxis_transform(), va='center', ha='left',
            fontsize=15, fontweight='bold', color=warna)

ax.set_yticks(ys, [s[0] for s in SKENARIO], fontsize=15, color=INK)
ax.set_xlim(SUMBU_MIN, SUMBU_MAX)
ax.set_ylim(-0.6, n - 0.4)
ticks = [round(SUMBU_MIN + 0.2 * k, 1) for k in range(int(round((SUMBU_MAX - SUMBU_MIN) / 0.2)) + 1)]
ticks = sorted({t for t in ticks if abs(t - KPI_HZ) >= 0.1} | {KPI_HZ})   # angka sumbu tidak menempel label KPI
ax.set_xticks(ticks, [koma(t, 1 if round(t, 1) == t else 2) for t in ticks], fontsize=13, color=MUTED)
for t, lab in zip(ticks, ax.get_xticklabels()):
    if t == KPI_HZ:
        lab.set_color(RED)
        lab.set_fontweight('bold')
ax.set_xlabel('frekuensi terendah (Hz)', fontsize=12, color=MUTED, loc='right')
ax.grid(axis='x', color=GRID, lw=1.2)
ax.set_axisbelow(True)
ax.tick_params(length=0, pad=10)
for s in ax.spines.values():
    s.set_visible(False)
if JUDUL:
    ax.set_title(JUDUL, loc='left', fontsize=20, fontweight='bold', color=NAVY, pad=14)

fig.tight_layout()
fig.subplots_adjust(right=0.80)
fig.savefig(OUT + '.svg', transparent=True)
fig.savefig(OUT + '.png', dpi=300, facecolor='white')
print('Tersimpan:', OUT + '.svg', 'dan', OUT + '.png')
