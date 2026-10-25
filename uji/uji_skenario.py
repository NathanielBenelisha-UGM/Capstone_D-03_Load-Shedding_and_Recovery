"""
uji_skenario.py - Menjalankan skenario gangguan ke app.py (HMI) dengan waktu yang presisi
=======================================================================================
Mengirim perintah lewat Socket.IO persis seperti tombol HMI (gen_control / toggle_line),
sehingga gangguan "simultan" (dt = 0) dan "beruntun sangat dekat" (dt = 0.5 s) bisa diuji
tanpa jeda dialog konfirmasi di browser. Selama uji, log HMI dan frekuensi yang terbaca app
dicatat lalu diringkas di akhir.

Jalankan saat PLC RUN, load.py dan app.py sudah berjalan:
    python uji/uji_skenario.py --gen PLTA                         # N-1 generator
    python uji/uji_skenario.py --gen PLTA --gen PLTGU --dt 0      # N-2 simultan
    python uji/uji_skenario.py --gen PLTS --gen PLTGU --dt 0.5    # N-2 beruntun 0,5 s
    python uji/uji_skenario.py --line Line_1-2                    # feeder trip
    python uji/uji_skenario.py --line Line_1-2 --gen PLTA --dt 0  # feeder + generator simultan
Opsi:
    --amati 30   lama pengamatan setelah gangguan (s)
    --pulih      setelah pengamatan, nyalakan kembali generator & feeder lalu amati restorasi
    --amati-pulih 420   lama pengamatan restorasi (s)
Catatan: nadir di sini = frekuensi terendah yang TERBACA app (sampel per siklus app). Nadir
sebenarnya ada di terminal load.py (kolom f=...), jadi simpan juga log load.py.
"""
import argparse
import time

import socketio

ap = argparse.ArgumentParser()
ap.add_argument('--url', default='http://localhost:5000')
ap.add_argument('--gen', action='append', default=[], help='PLTA / PLTS / PLTGU / PLTB (boleh 2x)')
ap.add_argument('--line', default=None, help='Line_1 / Line_1-1 / Line_1-2 / Line_2')
ap.add_argument('--dt', type=float, default=0.0, help='jeda kejadian kedua (s)')
ap.add_argument('--amati', type=float, default=30.0)
ap.add_argument('--pulih', action='store_true')
ap.add_argument('--amati-pulih', type=float, default=420.0)
args = ap.parse_args()

sio = socketio.Client()
T0 = None
rec = {'f': [], 'log': [], 'last': None}


@sio.on('grid_update')
def on_grid(data):
    t = time.perf_counter() - T0 if T0 else None
    rec['f'].append((t, data.get('frequency')))
    msg = data.get('log', '').split('] ', 1)[-1]
    if msg != rec['last']:
        rec['last'] = msg
        if t is not None and not msg.startswith(('Sistem Stabil', 'SISTEM NORMAL')):
            rec['log'].append((t, msg))


def kirim(kind, name, action):
    if kind == 'gen':
        sio.emit('gen_control', {'name': name, 'action': action})
    else:
        sio.emit('toggle_line', {'name': name, 'action': action})
    print(f"  t={time.perf_counter() - T0:6.3f}s  {kind} {name} {action}")


def ringkas(t_a, t_b, judul):
    fs = [(t, f) for t, f in rec['f'] if t is not None and t_a <= t <= t_b and f]
    if fs:
        t_min, f_min = min(fs, key=lambda x: x[1])
        f_max = max(f for _, f in fs)
        print(f"\n== {judul}: f terendah terbaca app {f_min:.2f} Hz (t={t_min:.1f}s) | f tertinggi {f_max:.2f} Hz | "
              f"f akhir {fs[-1][1]:.2f} Hz | KPI nadir > 49,50 Hz: {'LOLOS' if f_min > 49.5 else 'GAGAL'}")
    for t, m in rec['log']:
        if t_a <= t <= t_b:
            print(f"  t={t:6.1f}s  {m[:220]}")


sio.connect(args.url)
time.sleep(2.0)                    # tunggu beberapa grid_update agar kondisi awal terekam
T0 = time.perf_counter()
print(f"Mulai skenario: gen={args.gen} line={args.line} dt={args.dt}s")
events = []
if args.line:
    events.append((0.0, 'line', args.line))
    events += [(args.dt if i == 0 else args.dt, 'gen', g) for i, g in enumerate(args.gen)]
else:
    events += [(0.0 if i == 0 else args.dt, 'gen', g) for i, g in enumerate(args.gen)]
for t_ev, kind, name in sorted(events, key=lambda e: e[0]):
    while time.perf_counter() - T0 < t_ev:
        time.sleep(0.001)
    kirim(kind, name, 'OFF')
time.sleep(args.amati)
t_end = time.perf_counter() - T0
ringkas(0.0, t_end, "GANGGUAN")

if args.pulih:
    print("\nMenyalakan kembali generator & feeder untuk uji restorasi...")
    for g in args.gen:
        kirim('gen', g, 'ON')
    if args.line:
        kirim('line', args.line, 'ON')
    t_on = time.perf_counter() - T0
    time.sleep(args.amati_pulih)
    ringkas(t_on, time.perf_counter() - T0, "RESTORASI")
sio.disconnect()
