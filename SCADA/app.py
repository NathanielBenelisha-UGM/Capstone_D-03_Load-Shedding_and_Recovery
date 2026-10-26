"""
app.py - SCADA Master: monitoring PLC M241, MILP load shedding, restorasi, HMI web
==================================================================================
Siklus monitoring (0.1 s):
  1. Baca data PLC: %MW0-%MW54 (1 transaksi) + bit %M0-%M69 (1 transaksi)
  2. Evaluasi defisit -> UFLS / shedding defisit / feeder contingency / restorasi
  3. Tulis perintah trip beban ke PLC (hanya jika berubah) + evaluasi feedback PLC
  4. Kirim data ke HMI (Socket.IO)
"""
import copy
import os
import re
import threading
import time

from flask import Flask, render_template, request
from flask_socketio import SocketIO

from loadflow_module import (loads_on_buses, run_live_loadflow, solve_feeder_contingency_shedding,
                             solve_milp_shedding, solve_socp_fallback, verify_ac_post_shedding)
from plc_modbus import COIL_BASE_MW, M241Client as ModbusTcpClient

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# =========================================================
# KONFIGURASI PLC
# =========================================================
PLC_IP = os.getenv('PLC_IP', '192.168.100.200')
PORT   = int(os.getenv('PLC_PORT', 502))
client = ModbusTcpClient(PLC_IP, port=PORT)
plc_lock = threading.Lock()

# =========================================================
# DIAGNOSIS TRANSAKSI MODBUS (hanya mencatat; tidak mengubah logika)
# =========================================================
# Setiap transaksi app -> PLC melewati client.execute: dihitung per sumber (nama thread) beserta
# latensinya. Dicetak tiap 1 s selama MODBUS_DIAG_WINDOW_S setelah gangguan, dan kapan saja ada
# transaksi > MODBUS_DIAG_SLOW_MS. Transaksi load.py (proses lain) tidak ikut terhitung.
MODBUS_DIAG          = os.getenv('DIAG', '1') != '0'   # DIAG=0 -> pencatat transaksi Modbus nonaktif
MODBUS_DIAG_WINDOW_S = 15.0
MODBUS_DIAG_SLOW_MS  = 150.0
MODBUS_SOURCES = ('loop_utama', 'jalur_cepat', 'feedback', 'arming', 'loadflow', 'ufls_statis')
_mb_lock  = threading.Lock()
_mb_stats = {}                    # sumber -> [jumlah, total_ms, maks_ms]
_mb_watch = [0.0, 0.0]            # [waktu kejadian, aktif sampai] (time.monotonic)
_mb_execute = client.execute


def _counting_execute(*args, **kwargs):
    t0 = time.perf_counter()
    try:
        return _mb_execute(*args, **kwargs)
    finally:
        ms = (time.perf_counter() - t0) * 1000.0
        name = threading.current_thread().name
        src = name if name in MODBUS_SOURCES else 'lainnya'
        with _mb_lock:
            s = _mb_stats.setdefault(src, [0, 0.0, 0.0])
            s[0] += 1
            s[1] += ms
            s[2] = max(s[2], ms)


if MODBUS_DIAG:
    client.execute = _counting_execute


def modbus_watch_start():
    """Mulai / perpanjang jendela pencatatan diagnosis Modbus (dipanggil saat ada gangguan)."""
    now = time.monotonic()
    with _mb_lock:
        if now > _mb_watch[1]:
            _mb_watch[0] = now
        _mb_watch[1] = now + MODBUS_DIAG_WINDOW_S


def modbus_diag_worker():
    while True:
        time.sleep(1.0)
        with _mb_lock:
            snap = {k: list(v) for k, v in _mb_stats.items()}
            _mb_stats.clear()
            t_ev, until = _mb_watch
        total = sum(v[0] for v in snap.values())
        if not total:
            continue
        slowest = max(v[2] for v in snap.values())
        now = time.monotonic()
        if now > until and slowest < MODBUS_DIAG_SLOW_MS:
            continue
        label = f"t=+{now - t_ev:.0f}s" if now <= until else "LAMBAT"
        parts = ", ".join(f"{k} {v[0]} (maks {v[2]:.0f} ms)" for k, v in sorted(snap.items(), key=lambda kv: -kv[1][0]))
        print(f"[MODBUS] {label}: {total} req/s, latensi rata {sum(v[1] for v in snap.values()) / total:.0f} ms, "
              f"maks {slowest:.0f} ms | {parts}")

# =========================================================
# PETA ALAMAT MEMORY WORD PLC
# =========================================================
# %MW0  - %MW11 : Nilai beban aktual (ditulis load.py tiap siklus)
# %MW20 - %MW21 : Jam & Menit simulasi
# %MW30 - %MW33 : Sensor generasi PLTA/PLTS/PLTGU/PLTB (MW x10)
# %MW40 - %MW51 : Override per-channel dari HMI (0 = AUTO, >0 = nilai paksa x10)
# %MW54         : Frekuensi x100 (5000 = 50.00 Hz)
ADDR_SENSOR_LOAD = 0
ADDR_CLOCK       = 20
ADDR_SENSOR_GEN  = 30
ADDR_OVERRIDE    = 40
ADDR_FREQ        = 54
MW_BLOCK_COUNT   = ADDR_FREQ + 1          # %MW0-%MW54 dibaca dalam satu transaksi

# Bit %M (lewat adapter plc_modbus): status generator %M0-%M3 ... feedback %M60-%M69
BIT_BLOCK_COUNT  = 76   # %M0-%M75: status gen, perintah trip, READ_LOAD (60-69), tombol fisik (72-75)

# Panel tombol fisik generator: stasiun APBB-22 + relay penahan -> input I0-I3 -> ladder rung Gen 1-4
# (BTN_Gen1-4 -> SW_Gen1-4 = %MX20009.0-.3 = bit %M72-75). Bit = 1 selama relay menahan (ON).
# Hanya PERUBAHAN bit yang menjadi perintah (sama seperti klik ON/OFF generator di HMI).
PANEL_BUTTONS_ENABLED  = os.getenv('PANEL_BUTTONS', '1') != '0'   # PANEL_BUTTONS=0 -> bit tombol diabaikan
GEN_SWITCH_BITS        = {72: 'PLTA', 73: 'PLTS', 74: 'PLTGU', 75: 'PLTB'}
SWITCH_MISMATCH_WARN_S = 3.0   # posisi tombol != perintah generator selama ini -> peringatan sekali di log HMI

# =========================================================
# DATA GENERATOR & BEBAN
# =========================================================
# coil_read = status ONLINE (ditulis load.py), coil_write = perintah trip dari HMI
# min (MW), ramp (MW/s) & pfr (pita respons primer, MW) dipakai untuk cadangan restorasi
# -> SAMAKAN dengan GEN_CFG di Beban_Grid/load.py
GEN_MAP = {
    'PLTA':  {'coil_read': 0, 'coil_write': 50, 'mw_addr': 30, 'type': 'hydro',      'rated': 75, 'min': 15, 'ramp': 0.20, 'pfr': 7.5,  'droop': 0.05},
    'PLTS':  {'coil_read': 1, 'coil_write': 51, 'mw_addr': 31, 'type': 'solar_bess', 'rated': 25, 'min': 0,  'ramp': 12.5, 'pfr': 25.0, 'droop': 0.02},
    'PLTGU': {'coil_read': 2, 'coil_write': 52, 'mw_addr': 32, 'type': 'gas',        'rated': 75, 'min': 30, 'ramp': 0.08, 'pfr': 5.0,  'droop': 0.04},
    'PLTB':  {'coil_read': 3, 'coil_write': 53, 'mw_addr': 33, 'type': 'wind',       'rated': 25, 'min': 1,  'ramp': 0.06, 'pfr': 1.25, 'droop': 0.04},
}
GEN_ORDER = ['PLTA', 'PLTS', 'PLTGU', 'PLTB']

# Baris {'name': ..., 'priority': ...} di bawah diperbarui otomatis oleh
# persist_priority_to_file() saat operator mengubah prioritas dari HMI.
LOADS = [
    # %MW0-%MW5 -> Prioritas 2 (non-esensial)
    {'name': 'L101', 'priority': 2, 'default_priority': 2, 'coil_write': 21, 'mw_addr': 0, 'max_mw': 20},
    {'name': 'L201', 'priority': 2, 'default_priority': 2, 'coil_write': 22, 'mw_addr': 1, 'max_mw': 20},
    {'name': 'L303', 'priority': 2, 'default_priority': 2, 'coil_write': 23, 'mw_addr': 2, 'max_mw': 15},
    {'name': 'L304', 'priority': 2, 'default_priority': 2, 'coil_write': 24, 'mw_addr': 3, 'max_mw': 20},
    {'name': 'L305', 'priority': 2, 'default_priority': 2, 'coil_write': 25, 'mw_addr': 4, 'max_mw': 30},
    {'name': 'L401', 'priority': 2, 'default_priority': 2, 'coil_write': 26, 'mw_addr': 5, 'max_mw': 30},
    # %MW6-%MW8 -> Prioritas 3 (esensial)
    {'name': 'L301', 'priority': 3, 'default_priority': 3, 'coil_write': 31, 'mw_addr': 6, 'max_mw': 5},
    {'name': 'L302', 'priority': 3, 'default_priority': 3, 'coil_write': 32, 'mw_addr': 7, 'max_mw': 10},
    {'name': 'L402', 'priority': 3, 'default_priority': 3, 'coil_write': 33, 'mw_addr': 8, 'max_mw': 5},
    # %MW9-%MW11 -> Prioritas 4 (kritis)
    {'name': 'L403', 'priority': 4, 'default_priority': 4, 'coil_write': 41, 'mw_addr': 9, 'max_mw': 10},
    {'name': 'L404', 'priority': 4, 'default_priority': 4, 'coil_write': 42, 'mw_addr': 10, 'max_mw': 15},
    {'name': 'L405', 'priority': 4, 'default_priority': 4, 'coil_write': 43, 'mw_addr': 11, 'max_mw': 20},
]

# =========================================================
# FEEDBACK STATUS BEBAN DARI PLC M241 (READ_LOAD, %M60-%M69)
# =========================================================
# Ladder PLC menulis READ_LOADn = NOT perintah trip di setiap siklus scan.
# 9 beban punya output fisik sendiri (Q0-Q8). L403-L405 (Critical, bus 20 kV)
# berbagi satu output Q9 / READ_LOAD10 sebagai indikator kelompok.
FEEDBACK_COIL_BASE  = 60
FEEDBACK_INDEX = {
    'L101': 0, 'L201': 1, 'L301': 2, 'L302': 3, 'L303': 4,
    'L304': 5, 'L305': 6, 'L401': 7, 'L402': 8,
}
CRITICAL_GROUP       = ['L403', 'L404', 'L405']
CRITICAL_GROUP_INDEX = 9
FEEDBACK_MISMATCH_DELAY_S = 1.0   # toleransi sebelum beda perintah/feedback dianggap alarm

# Parameter kontrol (nilai sama seperti sebelumnya)
SHED_HOLDOFF_CYCLES     = 3     # jeda setelah melepas beban
RESTORE_STABLE_S        = 3.0   # frekuensi stabil (detik nyata) sebelum restorasi
RESTORE_COOLDOWN_S      = 4.0   # jeda antar penyalaan beban (detik nyata, tidak tergantung lama siklus)
RESTORE_F_MIN           = 49.95 # jendela frekuensi stabil untuk restorasi (Hz)
RESTORE_F_MAX           = 50.25
RESTORE_RAMP_S          = 50.0  # waktu beban yang dipulihkan naik penuh (soft-start load.py 2%/s)
RESTORE_MARGIN_MW       = 0.0   # margin cadangan di atas MW beban yang dipulihkan (dulu 3.0; 0 atas keputusan Niel, 25 Okt 2026)
RESTORE_RAMP_DONE       = 0.90  # beban dianggap sudah naik penuh pada >= 90% target
RESTORE_RAMP_TIMEOUT_S  = 100.0 # batas tunggu kenaikan beban sebelum lanjut
RESTORE_GUARD_HZ        = 49.60 # f di bawah ini selama beban yang dipulihkan masih naik -> beban itu dilepas lagi
                                # (dulu 49.80; 49.60 atas keputusan Niel: tetap di atas tahap 1 UFLS konvensional 49.50)
RESTORE_DF_HZ           = 0.15  # cadangan dihitung dari respons primer yang keluar sampai f turun sebesar ini (tetap di atas penjaga)
RESTORE_RETRY_S         = 60.0  # beban yang dibatalkan penjaga baru boleh dicoba lagi setelah jeda ini
SHED_RESERVE_MW         = 3.0   # cadangan minimum setelah shedding (rugi-rugi jaringan + noise beban)
SHED_TOL_MW             = 1.0   # kekurangan <= ini terhadap target dianggap tertangani (tidak menambah satu beban utuh)
SYNC_WINDOW_S           = 15.0  # lama sinkronisasi unit ke output minimum di load.py (min / 15 MW/s) untuk peringatan
UFLS_SUSTAIN_S          = 3.0   # f <= 49.25 Hz bertahan selama ini meski shedding "cukup" -> tahap UFLS bertunda
UFLS_STEP_FRAC          = 0.05  # besar tahap UFLS bertunda (fraksi demand)
GEN_MEMORY_FREEZE_S     = 10.0  # gen_memory dibekukan sejak generator pertama trip (cakup gangguan beruntun < 5 s)

# Jalur cepat pre-armed: tabel mask MILP dihitung sebelum gangguan, dikirim seketika saat generator trip
FAST_TRIP_ENABLED       = os.getenv('FAST_TRIP', '1') != '0'   # FAST_TRIP=0 -> hanya supervisor (pembanding)
FAST_POLL_S             = 0.02  # periode baca status generator (%M0-%M3 = %MW10000) oleh jalur cepat
ARM_REFRESH_S           = 5.0   # tabel mask dihitung ulang periodik (dan segera bila kondisi berubah)
ARM_MAX_AGE_S           = 10.0  # mask lebih tua dari ini tidak dipakai jalur cepat
FEEDBACK_POLL_S         = 0.02  # periode cek feedback READ_LOAD setelah perintah trip (pengukuran waktu)
AC_CHECK_S              = 1.0   # cek topologi AC rutin: ulang bila kondisi berubah atau tiap AC_CHECK_S
STATUS_PRINT_S          = 1.0   # baris status '| GEN:...' dicetak tiap STATUS_PRINT_S atau saat ada perubahan

# =========================================================
# STATE GLOBAL
# =========================================================
last_live_loads = []
load_memory     = {}
feedback_mismatch_since = {}
feedback_alarm_active   = set()
feedback_comm_ok_last   = True

app.line_statuses = {'Line_1': 'ONLINE', 'Line_1-1': 'ONLINE', 'Line_1-2': 'ONLINE', 'Line_2': 'ONLINE'}
app.priority_changed    = False
app.gen_memory          = {}
app.last_online_gens    = set()
app.gen_lost_pending    = False  # ada generator baru hilang (ONLINE->OFFLINE) yang belum dievaluasi MILP
app.gen_memory_frozen_until = 0.0  # time.monotonic() akhir jendela snapshot gen_memory
app.shed_holdoff        = 0
app.restore_timer       = 0      # time.monotonic() awal jendela frekuensi stabil (0 = belum mulai)
app.restore_cooldown    = 0      # time.monotonic() akhir jeda antar penyalaan (0 = tidak ada jeda)
app.restoring           = None   # beban terakhir yang dipulihkan & sedang naik: {'name', 'target', 't0'}
app.restore_retry       = {}     # beban -> time.monotonic() paling awal boleh dicoba lagi (setelah dibatalkan penjaga)
app.gen_word_seen       = (0.0, 0)   # (waktu baca, word %MW10000) dari loop utama, dipakai jalur cepat
app.gen_switch_prev     = None       # posisi tombol fisik siklus sebelumnya {gen: bool}; None = ambil acuan
app.gen_switch_mismatch = {}         # gen -> time.monotonic() awal posisi tombol != perintah generator
app.gen_switch_warned   = set()      # gen yang sudah diberi peringatan untuk episode tidak-sesuai ini
app.gen_panel           = None       # payload HMI: {gen: {'sw': bool, 'mismatch': bool}} atau None
app.plc_data_valid      = False      # pembacaan terakhir loop utama sah (lihat background_monitoring)
app.ufls_low_since      = None   # awal f <= 49.25 Hz (time.monotonic) untuk tahap UFLS bertunda
app.feeder_contingency  = False
app.feeder_stable_count = 0
app.lf_timer            = 0
app.status_print_t      = 0.0    # waktu terakhir baris status dicetak
app.status_print_state  = ''     # status breaker saat baris status terakhir dicetak
app.ac_cache            = None   # hasil cek topologi AC rutin terakhir: {'key', 't', 'res'}
app.last_breaker_state  = ''
app.contingency_matrix  = {}     # tampilan HMI: entri N-1 dari tabel mask pre-armed

# Satu-satunya sumber "beban yang diperintahkan lepas". Jalur cepat & supervisor hanya MENAMBAH
# (shed_add); beban hanya dinyalakan lewat restorasi bertahap / aksi operator (shed_remove, shed_replace).
# Urutan kunci: plc_lock lalu shed_lock (shed_lock tidak pernah dipegang saat mengambil plc_lock).
shed_lock = threading.Lock()
cmd_shed  = set()
shed_added_by = {}               # beban -> jalur yang menambahkannya (FAST / SUPERVISOR / OPERATOR)

# Tabel mask pre-armed: kunci tuple urutan generator yang hilang -> set beban yang harus lepas
arm_lock  = threading.Lock()
arm_event = threading.Event()    # minta tabel dihitung ulang segera
app.arm = {'table': {}, 'targets': {}, 'base_online': frozenset(), 'base_shed': frozenset(),
           'live_loads': [], 't': 0.0, 'calc_ms': 0.0}
app.arm_inputs = None            # snapshot kondisi terbaru dari loop utama untuk thread arming
app.feeder_shed = set()          # beban yang dilepas karena feeder/topologi (bukan karena generator hilang)
app.arm_key    = None

# =========================================================
# PENGUKURAN WAKTU SHEDDING (trip generator -> feedback PLC)
# =========================================================
# t_cmd    : perintah TRIP generator dari HMI diterima app.py
# t_detect : app.py membaca status generator OFFLINE dari PLC (ditulis load.py)
# t_trip   : perintah lepas beban hasil MILP selesai dikirim ke PLC
# t_fb     : bit READ_LOAD ladder PLC sudah sesuai perintah (dipolling tiap ~5 ms)
# Satu gangguan bisa punya beberapa TAHAP pelepasan (mis. N-2: tahap 1 defisit,
# tahap 2 UFLS). Setiap tahap dicatat sebagai satu baris di shedding_timing.csv.
TIMING_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'shedding_timing.csv')
TIMING_EVENT_WINDOW_S = 5.0     # gangguan dianggap selesai bila 5 s tanpa tahap/trip baru
gen_cmd_time     = {}           # generator -> waktu perintah TRIP dari HMI
shed_event       = None         # gangguan yang sedang diukur
last_cmd_shed    = set()        # perintah trip beban terakhir yang dikirim ke PLC
timing_log_queue = []           # pesan hasil untuk log HMI
prev_gen_online  = None


def timing_on_gen_offline(names):
    """Dipanggil saat generator terbaca OFFLINE: mulai / perluas gangguan."""
    global shed_event
    modbus_watch_start()
    now = time.perf_counter()
    if shed_event is None or now - shed_event['t_last'] > TIMING_EVENT_WINDOW_S:
        shed_event = {'gens': [], 't_cmd': None, 't_detect': now, 't_last': now, 'stages': 0,
                      'wall': time.strftime('%Y-%m-%d %H:%M:%S')}
    for name in names:
        if name not in shed_event['gens']:
            shed_event['gens'].append(name)
        t_cmd = gen_cmd_time.pop(name, None)
        if t_cmd is not None and (shed_event['t_cmd'] is None or t_cmd < shed_event['t_cmd']):
            shed_event['t_cmd'] = t_cmd
    shed_event['t_last'] = now


def timing_on_breaker_command(target_shed_set, source='SUPERVISOR'):
    """Dipanggil setiap perintah trip beban dikirim; tahap baru bila ada beban tambahan dilepas."""
    global last_cmd_shed
    newly_shed = set(target_shed_set) - last_cmd_shed
    last_cmd_shed = set(target_shed_set)
    event = shed_event
    if not newly_shed or event is None or time.perf_counter() - event['t_last'] > TIMING_EVENT_WINDOW_S:
        return
    event['stages'] += 1
    # jalur = yang MENAMBAHKAN beban baru (bisa beda dengan penulis: jalur cepat menambah, loop utama menulis)
    jalur = shed_added_by.get(sorted(newly_shed)[0], source)
    stage = {'n': event['stages'], 't_trip': time.perf_counter(), 'shed': set(target_shed_set), 'jalur': jalur}
    event['t_last'] = stage['t_trip']
    threading.Thread(target=_wait_plc_feedback, args=(event, stage), daemon=True, name='feedback').start()


def _feedback_matches(fb_bits, expected_shed):
    """Semua beban yang diperintahkan lepas di tahap ini sudah OFF (READ_LOAD=0). Beban lain diabaikan,
    karena tahap berikutnya (jalur cepat / supervisor) bisa sudah menambah beban sebelum feedback dicek."""
    for name, idx in FEEDBACK_INDEX.items():
        if name in expected_shed and bool(fb_bits[idx]):   # READ_LOAD=1 berarti beban masih ON
            return False
    return not (any(n in expected_shed for n in CRITICAL_GROUP) and bool(fb_bits[CRITICAL_GROUP_INDEX]))


def _wait_plc_feedback(event, stage):
    deadline = time.perf_counter() + 3.0
    stage['t_fb'] = None
    while time.perf_counter() < deadline:
        with plc_lock:
            r = client.read_coils(FEEDBACK_COIL_BASE, count=10)
        now = time.perf_counter()
        if not r.isError() and _feedback_matches(r.bits, stage['shed']):
            stage['t_fb'] = now
            break
        time.sleep(FEEDBACK_POLL_S)
    _record_stage(event, stage)


def _record_stage(event, stage):
    ms = lambda a, b: (b - a) * 1000.0 if a is not None and b is not None else None
    t0 = event['t_cmd'] if event['t_cmd'] is not None else event['t_detect']
    detect = ms(event['t_cmd'], event['t_detect']) if stage['n'] == 1 else None
    decide = ms(event['t_detect'], stage['t_trip']) if stage['n'] == 1 else None
    plc_fb = ms(stage['t_trip'], stage['t_fb'])
    total = ms(t0, stage['t_fb'])
    gens = '+'.join(event['gens'])
    shed = ' '.join(sorted(stage['shed']))
    jalur = stage.get('jalur', 'SUPERVISOR')
    fmt = lambda v: f"{v:.0f} ms" if v is not None else "-"
    if stage['t_fb'] is None:
        msg = f"WAKTU SHEDDING {gens} tahap {stage['n']} ({jalur}): feedback PLC tidak terkonfirmasi dalam 3 s (cek PLC RUN)."
    elif stage['n'] == 1:
        msg = (f"WAKTU SHEDDING {gens} tahap 1 ({jalur}): deteksi {fmt(detect)} + MILP/kirim {fmt(decide)} "
               f"+ PLC/feedback {fmt(plc_fb)} = total {fmt(total)} [{shed}]")
    else:
        msg = (f"WAKTU SHEDDING {gens} tahap {stage['n']} ({jalur}): PLC/feedback {fmt(plc_fb)}, "
               f"total sejak trip {fmt(total)} [{shed}]")
    print(f"[TIMING] {msg}")
    timing_log_queue.append(msg)
    try:
        header = "waktu,generator,tahap,jalur,deteksi_ms,milp_kirim_ms,plc_feedback_ms,total_ms,beban_dilepas\n"
        if os.path.exists(TIMING_CSV):
            with open(TIMING_CSV, encoding='utf-8') as f:
                old_header = f.readline()
            if old_header != header:   # format lama (tanpa kolom jalur): simpan terpisah, jangan dicampur
                os.replace(TIMING_CSV, TIMING_CSV.replace('.csv', f"_lama_{time.strftime('%Y%m%d_%H%M%S')}.csv"))
        new_file = not os.path.exists(TIMING_CSV)
        with open(TIMING_CSV, 'a', encoding='utf-8') as f:
            if new_file:
                f.write(header)
            vals = [f"{v:.1f}" if v is not None else "" for v in (detect, decide, plc_fb, total)]
            f.write(",".join([event['wall'], gens, str(stage['n']), jalur] + vals + [shed]) + "\n")
    except Exception as e:
        print(f"[TIMING] Gagal menulis CSV: {e}")


# =========================================================
# HELPER
# =========================================================
def get_load_pct(hour):
    """Profil beban harian (WBP/LWBP)."""
    profile = {0: 0.41, 4: 0.32, 6: 0.59, 9: 0.72,
               12: 0.77, 15: 0.72, 17: 0.81, 19: 0.90,
               21: 0.77, 23: 0.54, 24: 0.41}
    hs = sorted(profile.keys())
    for i in range(len(hs) - 1):
        if hs[i] <= hour <= hs[i + 1]:
            h1, h2, p1, p2 = hs[i], hs[i + 1], profile[hs[i]], profile[hs[i + 1]]
            return p1 if h2 == h1 else p1 + ((hour - h1) / (h2 - h1)) * (p2 - p1)
    return 0.45


def shed_snapshot():
    with shed_lock:
        return set(cmd_shed)


def _write_breakers(source, plc_bits=None):
    """Tulis cmd_shed TERBARU ke PLC (satu batch, hanya word yang berubah). Snapshot diambil di dalam
    plc_lock agar penulis yang lebih lambat tidak bisa menimpa trip yang baru ditambahkan thread lain.
    plc_bits: bit %M yang dibaca siklus ini. Bila bit trip di PLC sudah sama dengan cmd_shed, transaksi
    tulis (FC03 + FC16) dilewati; selisih yang muncul sesudah pembacaan terkoreksi siklus berikutnya."""
    with plc_lock:
        snap = shed_snapshot()
        want = {l['coil_write']: l['name'] in snap for l in LOADS}
        if plc_bits is None or any(plc_bits[b] != v for b, v in want.items()):
            client.write_coil_map(want)
        timing_on_breaker_command(snap, source)
    return snap


def shed_add(names, source='SUPERVISOR'):
    """Tambah beban ke cmd_shed (TIDAK PERNAH menyalakan) lalu kirim ke PLC. Return: beban yang baru lepas."""
    with shed_lock:
        new = set(names) - cmd_shed
        cmd_shed.update(new)
        shed_added_by.update(dict.fromkeys(new, source))
    if new:
        _write_breakers(source)
    return new


def shed_remove(name, source='RESTORASI'):
    """Nyalakan satu beban. HANYA untuk restorasi bertahap."""
    with shed_lock:
        cmd_shed.discard(name)
    app.feeder_shed.discard(name)
    _write_breakers(source)


def shed_replace(names, source='OPERATOR'):
    """Ganti seluruh set beban padam. HANYA untuk aksi operator (ubah prioritas)."""
    with shed_lock:
        cmd_shed.clear()
        cmd_shed.update(names)
        shed_added_by.update(dict.fromkeys(names, source))
    _write_breakers(source)


def shed_mw_of(shed_set, live_loads):
    return sum(l['mw'] for l in live_loads if l['name'] in shed_set)


def greedy_shed(target_mw, live_loads, base):
    """Cadangan bila MILP tidak Optimal (gagal solve / timeout): lepas beban prioritas terendah dulu
    (P2 -> P3 -> P4), pada prioritas sama yang terbesar dulu, sampai beban padam >= target.
    Beban dalam base tetap padam. Menggantikan 'lepas semua beban' / 'tidak melepas apa pun'."""
    shed = set(base)
    for l in sorted((l for l in live_loads if l['name'] not in shed),
                    key=lambda x: (x.get('priority', 2), -x['mw'])):
        if shed_mw_of(shed, live_loads) >= target_mw:
            break
        shed.add(l['name'])
    return shed


def ac_check_cached(gen_statuses, live_loads, shed):
    """Cek topologi AC rutin (verify_ac_post_shedding) yang hanya diulang bila generator ONLINE, beban padam,
    atau status saluran berubah, atau sudah AC_CHECK_S sejak cek terakhir (profil beban berubah perlahan)."""
    key = (frozenset(g['name'] for g in gen_statuses if g['status'] == 'ONLINE'), frozenset(shed),
           tuple(sorted(app.line_statuses.items())))
    now = time.monotonic()
    c = app.ac_cache
    if c is not None and c['key'] == key and now - c['t'] < AC_CHECK_S:
        return c['res']
    res = verify_ac_post_shedding(gen_statuses, live_loads, shed, app.line_statuses)
    app.ac_cache = {'key': key, 't': now, 'res': res}
    return res


def restore_headroom(gen_statuses):
    """Cadangan yang benar-benar bisa merespons selama beban yang dipulihkan naik (RESTORE_RAMP_S).

    Per generator ONLINE: min(rated - MW aktual, respons primer + ramp x RESTORE_RAMP_S). Respons primer hanya
    dihitung sebesar yang keluar saat f turun RESTORE_DF_HZ (droop: rated x df / (droop x 50), maks pita pfr),
    sehingga kriteria tidak mengandalkan penurunan f sampai di bawah penjaga RESTORE_GUARD_HZ.
    Generator yang masih sinkronisasi (MW aktual < min) belum dihitung. Return: (total_MW, rincian).
    """
    total, parts = 0.0, []
    for g in gen_statuses:
        if g['status'] != 'ONLINE':
            continue
        cfg = GEN_MAP[g['name']]
        if g['mw'] < cfg['min'] - 0.5:
            parts.append(f"{g['name']} sinkron")
            continue
        pfr_ok = min(cfg['pfr'], cfg['rated'] * RESTORE_DF_HZ / (cfg['droop'] * 50.0))
        hr = max(0.0, min(cfg['rated'] - g['mw'], pfr_ok + cfg['ramp'] * RESTORE_RAMP_S))
        total += hr
        parts.append(f"{g['name']} {hr:.1f}")
    return total, ", ".join(parts)


def shed_requirement(lost_gen_mw, demand_mw, capacity_mw, feeder_mw=0.0):
    """Target MW yang harus dilepas (sudah termasuk toleransi droop). Return: (target_MW, rincian).

    Target dibandingkan dengan SEMUA beban padam, jadi:
    - by_gen      : MW generator yang hilang dikurangi toleransi droop (governor boleh menyerap
                    sebagian defisit transien), DITAMBAH feeder_mw = beban yang padam karena feeder/topologi.
                    Beban itu sudah lepas sebelum generator trip (generasi sudah turun mengikutinya), jadi tidak
                    ikut menutup kehilangan generator.
    - by_capacity : demand - (kapasitas online - SHED_RESERVE_MW), TANPA toleransi: bila beban melebihi
                    kapasitas, generator yang tersisa sudah mentok rated dan tidak bisa menyerap apa pun.
                    Beban feeder sah mengurangi demand, jadi tidak ditambahkan di sini.
    """
    droop_tol = min(3.0, max(1.0, lost_gen_mw * 0.05)) if lost_gen_mw > 10.0 else 0.0
    feeder_mw = feeder_mw if lost_gen_mw > 0 else 0.0
    by_gen = max(0.0, lost_gen_mw - droop_tol) + feeder_mw
    by_capacity = max(0.0, demand_mw - (capacity_mw - SHED_RESERVE_MW))
    detail = (f"gen hilang {lost_gen_mw:.1f} - toleransi {droop_tol:.1f}"
              f"{f' + beban feeder {feeder_mw:.1f}' if feeder_mw > 0 else ''} = {by_gen:.1f}; "
              f"kapasitas {demand_mw:.1f} - ({capacity_mw:.0f} - {SHED_RESERVE_MW:.0f}) = {by_capacity:.1f}")
    return max(by_gen, by_capacity), detail


def shed_commit(candidate, base, target_mw, live_loads, source='SUPERVISOR', retries=3):
    """Commit optimistis set shedding yang dihitung dari `base` (snapshot cmd_shed saat MILP dimulai).

    Disimpan hanya bila cmd_shed masih sama dengan base. Bila jalur lain (jalur cepat / supervisor)
    sudah menambah beban di antaranya, tidak ada yang ditambah bila beban padam terbaru sudah memenuhi
    target; bila belum, MILP dihitung ulang terhadap cmd_shed terbaru. Mencegah dua jalur memilih
    kombinasi setara yang berbeda lalu tergabung (over-shedding).
    Return: (beban_baru, set_terpakai, dihitung_ulang)."""
    candidate, base, recomputed = set(candidate), set(base), False
    for _ in range(retries):
        with shed_lock:
            committed = cmd_shed == base
            if committed:
                new = candidate - cmd_shed
                cmd_shed.update(new)
                shed_added_by.update(dict.fromkeys(new, source))
            else:
                base = set(cmd_shed)
        if committed:
            if new:
                _write_breakers(source)
            return new, candidate | base, recomputed
        recomputed = True
        if shed_mw_of(base, live_loads) >= target_mw:
            return set(), base, recomputed
        res, status = solve_milp_shedding(target_mw, live_loads, base, droop_tolerance=0, keep_tripped=True)
        candidate = set(res) if status == 'Optimal' else candidate | base
    new = shed_add(candidate, source)       # percobaan habis (sangat jarang): tetap monoton
    return new, shed_snapshot(), recomputed


def ac_extend(shed_set, target_mw, live_loads, gen_statuses, line_statuses, freq_hz=None):
    """Validasi AC sebuah set shedding; bila melanggar, TAMBAH beban (MILP bus sensitivity / SOCP).
    Beban dalam shed_set tidak pernah dikeluarkan. Return: (set_final, catatan)."""
    is_valid, viol_buses = verify_ac_post_shedding(gen_statuses, live_loads, shed_set, line_statuses)
    if is_valid:
        return set(shed_set), ""
    if len(viol_buses) <= 2 and "ALL_BUSES" not in viol_buses:
        adjusted, status = solve_milp_shedding(target_mw, live_loads, shed_set, droop_tolerance=0, keep_tripped=True,
                                               must_shed_loads=loads_on_buses(viol_buses), freq_hz=freq_hz)
        note = " [Post-AC: tambah beban di bus terkendala]"
        if status != 'Optimal':
            adjusted = set(shed_set)
    else:
        socp_set, socp_status = solve_socp_fallback(gen_statuses, live_loads, shed_set, line_statuses)
        adjusted = socp_set if socp_status in ("optimal", "optimal_inaccurate") else set(shed_set)
        note = " [Post-AC: tambah beban via SOCP]"
    return set(adjusted) | set(shed_set), note


def ac_validate_and_extend(new_shed_set, target_mw, live_loads, gen_statuses, freq_hz, print_tag=None):
    """Validasi AC pasca-shedding; bila melanggar, tambah beban dan kirim ke PLC (tidak pernah menukar)."""
    adjusted, note = ac_extend(new_shed_set, target_mw, live_loads, gen_statuses, app.line_statuses, freq_hz)
    if adjusted != set(new_shed_set):
        shed_add(adjusted)
        if print_tag:
            print(f"[AC EXTEND {print_tag}] Beban ditambah pasca-verifikasi AC: {sorted(adjusted - set(new_shed_set))}")
    return adjusted, note


def evaluate_plc_feedback(load_statuses, fb_bits):
    """Isi 'plc_status', 'fb_source', 'fb_mismatch' tiap beban dari bit READ_LOAD PLC.

    'status' tetap keputusan MILP (dipakai algoritma); HMI menampilkan 'plc_status'.
    Return: (pesan_log atau None, info feedback untuk HMI).
    """
    global feedback_comm_ok_last
    now = time.time()

    if fb_bits is None:
        for ls in load_statuses:
            ls.update(plc_status='UNKNOWN', fb_source='NONE', fb_mismatch=False)
        feedback_mismatch_since.clear()
        feedback_alarm_active.clear()
        msg = "FEEDBACK PLC TIDAK TERBACA: status beban UNKNOWN (cek koneksi PLC)." if feedback_comm_ok_last else None
        feedback_comm_ok_last = False
        return msg, {'ok': False, 'critical_group_on': None}

    msg = None if feedback_comm_ok_last else "FEEDBACK PLC PULIH: status beban kembali dibaca dari PLC."
    feedback_comm_ok_last = True

    group_cmd_on = not any(ls['status'] == 'TRIPPED' for ls in load_statuses if ls['id'] in CRITICAL_GROUP)
    group_fb_on = bool(fb_bits[CRITICAL_GROUP_INDEX])
    new_alarms, cleared = [], []

    for ls in load_statuses:
        name = ls['id']
        cmd_on = ls['status'] != 'TRIPPED'
        if name in FEEDBACK_INDEX:
            fb_on = bool(fb_bits[FEEDBACK_INDEX[name]])
            ls.update(plc_status='NORMAL' if fb_on else 'TRIPPED', fb_source='PLC')
            differ = fb_on != cmd_on
        elif name in CRITICAL_GROUP:
            ls.update(plc_status=ls['status'], fb_source='GROUP')   # dicek lewat indikator Q9
            differ = group_fb_on != group_cmd_on
        else:
            ls.update(plc_status=ls['status'], fb_source='NONE')
            differ = False

        if differ:
            mismatch = (now - feedback_mismatch_since.setdefault(name, now)) >= FEEDBACK_MISMATCH_DELAY_S
        else:
            feedback_mismatch_since.pop(name, None)
            mismatch = False
        ls['fb_mismatch'] = mismatch

        if mismatch and name not in feedback_alarm_active:
            feedback_alarm_active.add(name)
            new_alarms.append(name)
        elif not mismatch and name in feedback_alarm_active:
            feedback_alarm_active.discard(name)
            cleared.append(name)

    if new_alarms:
        msg = ("FEEDBACK MISMATCH: " + ", ".join(sorted(new_alarms)) +
               " - perintah SCADA tidak sesuai status output PLC.")
    elif cleared and not feedback_alarm_active:
        msg = "FEEDBACK NORMAL: perintah SCADA dan status output PLC kembali sesuai."
    return msg, {'ok': True, 'critical_group_on': group_fb_on}


def restoration_step(shed_set, live_loads, gen_statuses, freq_hz, keep_mw=0.0):
    """Satu langkah restorasi bertahap (dipanggil tiap siklus saat restorasi diizinkan).

    keep_mw = MW yang WAJIB tetap padam: 0 bila kapasitas cukup (semua generator kembali), atau target
    shedding kondisi saat ini bila sebagian generator masih lepas (restorasi parsial). Satu beban
    dinyalakan hanya bila: frekuensi stabil, beban sebelumnya sudah naik penuh, cadangan cepat cukup,
    validasi AC lolos, dan sisa beban padam tetap >= keep_mw. Saat f > RESTORE_F_MAX (generasi berlebih)
    berlaku restorasi korektif: beban <= surplus generasi terukur boleh dinyalakan. Return: (shed_set, log_msg)."""
    shed_set = set(shed_set)
    mode = "RESTORASI PARSIAL" if keep_mw > 0 else "RESTORASI"

    if app.restoring:
        # Beban yang baru dipulihkan naik bertahap (soft-start): tunggu sampai penuh
        # agar generator lambat sempat mengikuti sebelum beban berikutnya dinyalakan
        r = app.restoring
        r_now = next((l['actual_mw'] for l in live_loads if l['name'] == r['name']), 0.0)
        waited = time.monotonic() - r['t0']
        app.restore_timer = 0
        if r['name'] in shed_set:
            app.restoring = None
            return shed_set, f"{mode}: {r['name']} dilepas kembali, evaluasi ulang restorasi."
        if freq_hz < RESTORE_GUARD_HZ:
            # Penjaga: generator tidak mampu mengikuti kenaikan beban (ramp + respons primer kurang) -> batalkan
            shed_add({r['name']}, 'RESTORASI')
            app.restoring = None
            app.restore_retry[r['name']] = time.monotonic() + RESTORE_RETRY_S
            print(f"[{mode} DIBATALKAN] {r['name']} dilepas lagi: f={freq_hz:.2f}Hz < {RESTORE_GUARD_HZ:.2f}Hz saat beban naik "
                  f"({r_now:.1f}/{r['target']:.1f}MW, {waited:.0f}s). Coba lagi >= {RESTORE_RETRY_S:.0f}s.")
            return shed_set | {r['name']}, (f"{mode} DIBATALKAN: {r['name']} dilepas lagi (f={freq_hz:.2f}Hz < "
                                            f"{RESTORE_GUARD_HZ:.2f}Hz saat beban naik). Coba lagi {RESTORE_RETRY_S:.0f}s.")
        if r_now >= RESTORE_RAMP_DONE * r['target'] or waited >= RESTORE_RAMP_TIMEOUT_S:
            app.restoring = None
            return shed_set, (f"{mode}: {r['name']} sudah naik ({r_now:.1f}/{r['target']:.1f}MW, "
                              f"{waited:.0f}s). Evaluasi beban berikutnya ({len(shed_set)} tersisa).")
        return shed_set, (f"{mode} BERTAHAP: Menunggu {r['name']} naik penuh "
                          f"({r_now:.1f}/{r['target']:.1f}MW, {waited:.0f}s, f={freq_hz:.2f}Hz, "
                          f"sisa {len(shed_set)} beban mati).")

    now_m = time.monotonic()
    if now_m < app.restore_cooldown:
        # Jeda setelah menyalakan satu beban
        return shed_set, (f"{mode} BERTAHAP: Menstabilkan frekuensi & governor "
                          f"({app.restore_cooldown - now_m:.1f}s lagi, f={freq_hz:.2f}Hz, sisa {len(shed_set)} beban mati).")

    # Restorasi KOREKTIF: f di atas jendela karena generasi berlebih (mis. unit sinkron di output minimum saat
    # beban kecil) -> menyalakan beban justru menurunkan f. Hanya beban <= surplus terukur (gen - beban), tanpa
    # menunggu f stabil; syarat lain (satu per langkah, keep_mw, AC, penjaga) tetap.
    surplus_mw = None
    if freq_hz > RESTORE_F_MAX:
        surplus_mw = (sum(g['mw'] for g in gen_statuses if g['status'] == 'ONLINE')
                      - sum(l['actual_mw'] for l in live_loads if l['name'] not in shed_set))
        app.restore_timer = 0
        if surplus_mw <= 0:
            return shed_set, (f"{mode} DITAHAN: f={freq_hz:.2f}Hz di atas {RESTORE_F_MAX:.2f}Hz tanpa surplus "
                              f"generasi terukur ({surplus_mw:.1f}MW).")
        mode += " KOREKTIF"
    elif freq_hz < RESTORE_F_MIN:
        app.restore_timer = 0
        return shed_set, (f"{mode} DITAHAN: Menunggu frekuensi stabil di "
                          f"{RESTORE_F_MIN:.2f}–{RESTORE_F_MAX:.2f} Hz (saat ini {freq_hz:.2f}Hz).")
    else:
        if app.restore_timer == 0:
            app.restore_timer = now_m
        stable_s = now_m - app.restore_timer
        if stable_s < RESTORE_STABLE_S:
            return shed_set, (f"{mode}: Memverifikasi stabilitas sistem... "
                              f"({stable_s:.1f}s / {RESTORE_STABLE_S:.1f}s, "
                              f"f={freq_hz:.2f}Hz, {len(shed_set)} beban menunggu).")
        app.restore_timer = 0

    # Nyalakan tepat 1 beban: prioritas tertinggi dulu (4 -> 3 -> 2),
    # pada prioritas sama yang terkecil dulu (ramah governor)
    shed_candidates = sorted((l for l in live_loads if l['name'] in shed_set),
                             key=lambda x: (-x.get('priority', 2), x.get('nominal_mw', x.get('mw', 0))))
    # Cadangan cepat (headroom aktual dibatasi ramp), bukan rated - demand:
    # generator yang baru sinkron / ramp lambat tidak bisa langsung menanggung beban
    reserve, reserve_detail = restore_headroom(gen_statuses)
    log_msg = f"{mode} TERTUNDA: tidak ada beban yang memenuhi syarat."
    for cand in shed_candidates:
        cand_mw = load_memory.get(cand['name'], cand.get('nominal_mw', cand['max_mw']))
        proposed = shed_set - {cand['name']}
        if app.restore_retry.get(cand['name'], 0.0) > now_m:
            log_msg = (f"{mode} TERTUNDA: {cand['name']} baru dibatalkan penjaga frekuensi "
                       f"(coba lagi {app.restore_retry[cand['name']] - now_m:.0f}s).")
            continue
        if shed_mw_of(proposed, live_loads) < keep_mw:
            log_msg = (f"{mode} TERTUNDA: {cand['name']} masih dibutuhkan "
                       f"(sisa padam {shed_mw_of(proposed, live_loads):.1f}MW < target {keep_mw:.1f}MW).")
            continue
        if surplus_mw is not None:
            if cand_mw > surplus_mw:
                log_msg = (f"{mode} TERTUNDA: {cand['name']} ({cand_mw:.1f}MW) lebih besar dari surplus generasi "
                           f"{surplus_mw:.1f}MW (f={freq_hz:.2f}Hz).")
                continue
        elif (cand_mw + RESTORE_MARGIN_MW) > reserve:
            log_msg = (f"{mode} TERTUNDA: Cadangan cepat {reserve:.1f}MW ({reserve_detail}) "
                       f"belum cukup untuk {cand['name']} ({cand_mw:.1f}MW + margin {RESTORE_MARGIN_MW:.1f}MW).")
            continue
        if not verify_ac_post_shedding(gen_statuses, live_loads, proposed, app.line_statuses)[0]:
            log_msg = f"{mode} TERTUNDA: {cand['name']} memicu kendala tegangan/saluran pada AC Load Flow."
            continue
        shed_remove(cand['name'])
        tag = f"{cand['name']} (P{cand['priority']}"
        basis = (f"Surplus generasi {surplus_mw:.1f}MW, f={freq_hz:.2f}Hz" if surplus_mw is not None
                 else f"Cadangan cepat {reserve:.1f}MW")
        print(f"[{mode} BERTAHAP] Breaker {tag}, {cand_mw:.1f}MW) DINYALAKAN. {basis}"
              f"{f', sisa padam {shed_mw_of(proposed, live_loads):.1f}MW >= target {keep_mw:.1f}MW' if keep_mw > 0 else ''}.")
        app.restore_cooldown = time.monotonic() + RESTORE_COOLDOWN_S
        app.restoring = {'name': cand['name'], 'target': cand_mw, 't0': time.monotonic()}
        if proposed:
            return proposed, (f"{mode}: Menyalakan {tag}, {cand_mw:.0f}MW). "
                              f"Menunggu beban naik penuh sebelum beban berikutnya ({len(proposed)} tersisa).")
        return proposed, f"RESTORASI LENGKAP: Menyalakan {tag}, {cand_mw:.0f}MW). Seluruh beban berhasil pulih 100%!"
    return shed_set, log_msg


# =========================================================
# SOCKET - DETEKSI KONEKSI
# =========================================================
@socketio.on('connect')
def handle_connect():
    print(">>> WEB BROWSER TERHUBUNG KE SERVER <<<")


# =========================================================
# PENGUKURAN WAKTU SIKLUS NYATA
# =========================================================
class CycleMeter:
    """Mengukur waktu siklus nyata (proses + sleep), ringkasan dicetak tiap `report_s` detik.

    start() di awal siklus, work_done() tepat sebelum sleep, end() setelah sleep.
    Siklus yang tidak sampai end() (mis. menunggu koneksi PLC) tidak dihitung.
    Faktor waktu = nominal / siklus nyata (1.00x = sesuai asumsi 0.1 s per siklus).
    """

    def __init__(self, tag, nominal_s, report_s=10.0):
        self.tag, self.nominal_s, self.report_s = tag, nominal_s, report_s
        self.t0 = self.t_work = None
        self.cycles, self.works = [], []
        self.t_report = time.perf_counter()

    def start(self):
        self.t0 = time.perf_counter()

    def work_done(self):
        self.t_work = time.perf_counter()

    def end(self):
        now = time.perf_counter()
        if self.t0 is None or self.t_work is None:
            return
        self.cycles.append(now - self.t0)
        self.works.append(self.t_work - self.t0)
        self.t0 = self.t_work = None
        if now - self.t_report >= self.report_s:
            self._report(now)

    def _report(self, now):
        n = len(self.cycles)
        srt = sorted(self.cycles)
        avg = sum(srt) / n
        p95 = srt[min(n - 1, int(0.95 * n))]
        print(f"[CYCLE {self.tag}] {n} siklus / {now - self.t_report:.1f} s | "
              f"siklus rata-rata {avg * 1000:.0f} ms (min {srt[0] * 1000:.0f}, p95 {p95 * 1000:.0f}, "
              f"maks {srt[-1] * 1000:.0f}) | proses {sum(self.works) / n * 1000:.0f} ms | "
              f"faktor waktu {self.nominal_s / avg:.2f}x")
        self.cycles, self.works = [], []
        self.t_report = now


class StepCycleMeter(CycleMeter):
    """CycleMeter + waktu per langkah siklus.

    mark(nama) mencatat durasi sejak penanda sebelumnya (atau sejak start()).
    Siklus >= `slow_s` langsung dicetak rinciannya sebagai [SLOW CYCLE].
    """

    def __init__(self, tag, nominal_s, steps, slow_s=0.4, report_s=10.0):
        super().__init__(tag, nominal_s, report_s)
        self.steps, self.slow_s = steps, slow_s
        self.cur, self.t_mark = {}, None
        self.hist = {s: [] for s in steps}

    def start(self):
        super().start()
        self.cur, self.t_mark = {}, self.t0

    def mark(self, name):
        now = time.perf_counter()
        if self.t_mark is not None:
            self.cur[name] = self.cur.get(name, 0.0) + (now - self.t_mark)
        self.t_mark = now

    def end(self):
        if self.t0 is not None and self.t_work is not None:
            cycle = time.perf_counter() - self.t0
            work = self.t_work - self.t0
            for s in self.steps:
                if s in self.cur:
                    self.hist[s].append(self.cur[s])
            if cycle >= self.slow_s:
                parts = " | ".join(f"{s} {self.cur[s] * 1000:.0f}" for s in self.steps if s in self.cur)
                other = work - sum(self.cur.values())
                print(f"[SLOW CYCLE {self.tag}] {cycle * 1000:.0f} ms | {parts} | lainnya {other * 1000:.0f} "
                      f"| sleep {(cycle - work) * 1000:.0f}")
        super().end()

    def _report(self, now):
        super()._report(now)
        parts = " | ".join(f"{s} {sum(v) / len(v) * 1000:.0f}/{max(v) * 1000:.0f}"
                           for s, v in self.hist.items() if v)
        print(f"[STEP {self.tag}] rata-rata/maks (ms): {parts}")
        self.hist = {s: [] for s in self.steps}


# timer restorasi & pesan log menganggap 0.1 s per siklus
cycle_meter = StepCycleMeter('app.py', 0.1, ['baca_plc', 'olah_data', 'keputusan', 'matriks_n1',
                                             'kirim_feedback', 'emit'])


# =========================================================
# BACKGROUND MONITORING
# =========================================================
def background_monitoring():
    global last_live_loads, prev_gen_online
    print("--- Background Monitoring Dimulai ---")
    while True:
        cycle_meter.start()
        try:
            with plc_lock:
                if not client.connect():
                    print("Menunggu koneksi ke PLC...")
                    time.sleep(2)
                    continue

            # ── 1. Baca PLC: %MW0-%MW54 dan bit %M0-%M69 (2 transaksi)
            with plc_lock:
                mw_res = client.read_holding_registers(address=0, count=MW_BLOCK_COUNT)
                bit_res = client.read_coils(0, count=BIT_BLOCK_COUNT)
            mw = None if mw_res.isError() else mw_res.registers
            bits = None if bit_res.isError() else bit_res.bits
            if mw is None or bits is None or mw[ADDR_FREQ] == 0:
                # Data PLC tidak sah: transaksi gagal, atau PLC baru restart dan belum ditulis load.py
                # (semua bit 0 terbaca "semua generator OFFLINE"). Tidak ada keputusan shedding dari data ini.
                if app.plc_data_valid:
                    print("[PLC DATA] Tidak sah (baca gagal / PLC baru restart, %MW54 = 0): keputusan ditahan.")
                app.plc_data_valid = False
                app.gen_switch_prev = None   # PLC restart / putus: posisi tombol diambil ulang sebagai acuan
                time.sleep(0.1)
                continue
            if not app.plc_data_valid:
                print("[PLC DATA] Sah kembali.")
            app.plc_data_valid = True
            # Word status generator %MW10000 ikut dipakai jalur cepat (menghemat satu poll)
            app.gen_word_seen = (time.perf_counter(), sum(1 << i for i in range(16) if bits[i]))
            fb_bits = bits[FEEDBACK_COIL_BASE:FEEDBACK_COIL_BASE + 10] if bits else None
            handle_gen_switches(bits)   # di luar plc_lock (handle_gen_control mengambil plc_lock sendiri)
            cycle_meter.mark('baca_plc')

            # Generator: daya aktual %MW30-33 + status %M0-3
            gen_mw = {name: (mw[ADDR_SENSOR_GEN + i] / 10.0 if mw else 0) for i, name in enumerate(GEN_ORDER)}
            gen_statuses = []
            for name in GEN_ORDER:
                is_on = bool(bits and bits[GEN_MAP[name]['coil_read']])
                gen_statuses.append({'name': name, 'type': GEN_MAP[name]['type'], 'mw': gen_mw[name],
                                     'rated': GEN_MAP[name]['rated'],
                                     'status': 'ONLINE' if is_on else 'OFFLINE'})
            total_gen = sum(gen_mw.values())

            # Generator yang baru terbaca OFFLINE: pengukuran waktu shedding + snapshot pra-gangguan
            gen_online_now = {g['name'] for g in gen_statuses if g['status'] == 'ONLINE'}
            if prev_gen_online is not None and prev_gen_online - gen_online_now:
                timing_on_gen_offline(sorted(prev_gen_online - gen_online_now))
                app.gen_lost_pending = True   # harus dievaluasi walau sedang holdoff
                if time.monotonic() >= app.gen_memory_frozen_until:
                    app.gen_memory_frozen_until = time.monotonic() + GEN_MEMORY_FREEZE_S
            prev_gen_online = gen_online_now

            # Memori daya aktual generator (dipakai sebagai "MW generator hilang" saat trip).
            # Dibekukan selama jendela gangguan agar respons primer generator lain tidak ikut
            # terhitung (snapshot pra-gangguan), lalu kembali diperbarui.
            if time.monotonic() >= app.gen_memory_frozen_until:
                for g in gen_statuses:
                    if g['status'] == 'ONLINE' and g['mw'] > 1.0:
                        app.gen_memory[g['name']] = g['mw']

            # Frekuensi %MW54 & jam simulasi %MW20-21
            freq_hz = mw[ADDR_FREQ] / 100.0 if mw else 50.0
            if mw:
                plc_hour, plc_min = mw[ADDR_CLOCK], mw[ADDR_CLOCK + 1]
                plc_time = f"{plc_hour:02d}:{plc_min:02d}"
                sim_hour = (plc_hour + plc_min / 60.0) % 24
            else:
                plc_time = time.strftime('%H:%M')
                sim_hour = 19.0
            load_pct = get_load_pct(sim_hour)
            override = mw[ADDR_OVERRIDE:ADDR_OVERRIDE + 12] if mw else [0] * 12

            current_tripped = shed_snapshot()   # termasuk trip dari jalur cepat

            # ── 2. Beban %MW0-11
            if mw:
                live_loads = []
                for i, load in enumerate(LOADS):
                    actual_mw = mw[ADDR_SENSOR_LOAD + i] / 10.0
                    if override[i] > 0:
                        nominal_mw = min(override[i] / 10.0, load['max_mw'])
                    else:
                        nominal_mw = load['max_mw'] * load_pct
                    if load['name'] in current_tripped:
                        # Beban padam: pakai daya aktual terakhir sebelum trip
                        eff_mw = load_memory.get(load['name'], nominal_mw)
                    else:
                        # Beban ONLINE: pakai daya aktual sensor saat ini
                        eff_mw = actual_mw
                        if actual_mw > 0.5:
                            load_memory[load['name']] = actual_mw
                    live_loads.append({**load, 'mw': eff_mw, 'actual_mw': actual_mw, 'nominal_mw': nominal_mw})
                last_live_loads = live_loads
            elif last_live_loads:
                live_loads = last_live_loads
            else:
                time.sleep(2)
                continue

            # ── 3. Evaluasi kapasitas
            potential_total_demand = sum(l['mw'] for l in live_loads)
            online_gens = [g for g in gen_statuses if g['status'] == 'ONLINE']
            available_capacity = sum(g['rated'] for g in online_gens)
            # Defisit nyata = daya generator yang lepas (memori) atau kelebihan demand
            lost_gen_mw = sum(app.gen_memory.get(g['name'], g['rated'] * 0.9)
                              for g in gen_statuses if g['status'] == 'OFFLINE')
            capacity_deficit = max(lost_gen_mw, max(0.0, potential_total_demand - available_capacity))
            currently_shed_mw = shed_mw_of(current_tripped, live_loads)

            shed_set = set()
            log_msg = "Sistem Stabil."

            # Feeder kembali online -> reset counter, izinkan restorasi penuh
            feeder_offline = any(s == 'OFFLINE' for s in app.line_statuses.values())
            if not feeder_offline and app.feeder_contingency:
                app.feeder_stable_count = 0
                app.restore_timer = 0
                app.restore_cooldown = 0
                print("[FEEDER RESTORED] Semua feeder kembali — mode normal, restorasi penuh dimulai.")
                if app.feeder_shed:
                    print(f"[FEEDER RESTORED] {sorted(app.feeder_shed)} tidak lagi dihitung sebagai beban feeder.")
                app.feeder_shed.clear()
            app.feeder_contingency = feeder_offline
            cycle_meter.mark('olah_data')

            # Target shedding: toleransi droop hanya untuk MW generator hilang, bukan kekurangan kapasitas
            feeder_mw = shed_mw_of(app.feeder_shed & current_tripped, live_loads)
            shed_target, shed_target_detail = shed_requirement(lost_gen_mw, potential_total_demand, available_capacity,
                                                               feeder_mw)
            if freq_hz > 49.25:
                app.ufls_low_since = None

            # ── 4a. OVER-FREQUENCY: diserahkan ke governor droop (tanpa menyalakan beban mendadak)
            if freq_hz > 51.5:
                shed_set = current_tripped.copy()
                log_msg = f"OVER-FREQUENCY ({freq_hz:.2f}Hz): Governor droop meredam kelebihan generasi daya secara alami."
                if shed_set and app.shed_holdoff == 0:
                    shed_set, log_msg = restoration_step(shed_set, live_loads, gen_statuses, freq_hz,
                                                         keep_mw=shed_target if capacity_deficit > 0 else 0.0)
                    log_msg = f"OVER-FREQUENCY ({freq_hz:.2f}Hz): {log_msg}"

            # ── 4S. BASELINE UFLS STATIS: pelepasan hanya oleh relai frekuensi (ufls_static_watch).
            #       Restorasi parsial memakai logika yang sama dengan skema optimal.
            elif STATIC_SCHEME and (freq_hz <= 49.25 or capacity_deficit > 0):
                shed_set = current_tripped.copy()
                app.gen_lost_pending = False
                surplus = currently_shed_mw - shed_target
                smallest = min((l['mw'] for l in live_loads if l['name'] in shed_set), default=None)
                # app.restoring: beban yang baru dipulihkan masih naik -> restoration_step tetap dipanggil (penjaga aktif)
                if freq_hz > 49.25 and ((smallest is not None and surplus >= smallest) or app.restoring):
                    shed_set, log_msg = restoration_step(shed_set, live_loads, gen_statuses, freq_hz, keep_mw=shed_target)
                else:
                    app.restore_timer = 0
                    log_msg = (f"UFLS STATIS: {len(shed_set)} beban padam oleh relai frekuensi "
                               f"({currently_shed_mw:.0f}MW, f={freq_hz:.2f}Hz).")

            # ── 4b. UFLS (frekuensi <= 49.25 Hz)
            elif freq_hz <= 49.25:
                if capacity_deficit == 0:
                    # Gangguan frekuensi tanpa generator trip (misal beban melonjak)
                    calc_deficit = max(currently_shed_mw + potential_total_demand * 0.1, potential_total_demand * 0.1)
                    droop_tol = min(3.0, max(1.0, calc_deficit * 0.05)) if calc_deficit > 10.0 else 0.0
                    effective_deficit = max(0.0, calc_deficit - droop_tol)
                else:
                    effective_deficit = shed_target

                # Tahap UFLS bertunda: f tetap <= 49.25 Hz selama UFLS_SUSTAIN_S walau shedding dianggap cukup
                now_m = time.monotonic()
                if app.ufls_low_since is None:
                    app.ufls_low_since = now_m
                low_for = now_m - app.ufls_low_since
                sustained = low_for >= UFLS_SUSTAIN_S

                if currently_shed_mw >= effective_deficit and app.shed_holdoff > 0:
                    app.shed_holdoff -= 1
                    shed_set = current_tripped.copy()
                    log_msg = (f"DEFISIT TERTANGANI. Menahan kondisi pasca-pemotongan "
                               f"({app.shed_holdoff} siklus lagi, {shed_mw_of(shed_set, live_loads):.0f}MW).")
                elif currently_shed_mw >= effective_deficit and freq_hz > 48.0 and not sustained:
                    # Sudah cukup: kunci kondisi trip (anti-osilasi)
                    shed_set = current_tripped.copy()
                    log_msg = (f"DEFISIT TERTANGANI. Mempertahankan {len(shed_set)} beban mati "
                               f"({shed_mw_of(shed_set, live_loads):.0f}MW, f<=49.25Hz selama {low_for:.1f}s).")
                else:
                    stage_note = ""
                    if currently_shed_mw >= effective_deficit and sustained:
                        effective_deficit = currently_shed_mw + UFLS_STEP_FRAC * potential_total_demand
                        stage_note = f" [UFLS bertunda: f<=49.25Hz selama {low_for:.1f}s, tambah {UFLS_STEP_FRAC * 100:.0f}% demand]"
                        app.ufls_low_since = now_m
                    app.gen_lost_pending = False
                    print(f"[TARGET SHED] {effective_deficit:.1f} MW ({shed_target_detail}){stage_note}")
                    new_shed_set, milp_status = solve_milp_shedding(effective_deficit, live_loads, current_tripped,
                                                                    freq_hz=freq_hz, droop_tolerance=0, keep_tripped=True,
                                                                    label="UFLS: frekuensi <= 49.25 Hz")
                    log_extra = ""
                    if milp_status != 'Optimal':
                        new_shed_set = greedy_shed(effective_deficit, live_loads, current_tripped)
                        print(f"[UFLS FALLBACK] MILP {milp_status}: greedy prioritas -> {sorted(new_shed_set)}")
                        shed_add(new_shed_set)
                        new_shed_set, log_extra = ac_validate_and_extend(
                            new_shed_set, effective_deficit, live_loads, gen_statuses, freq_hz, "UFLS")
                        log_extra = f" [MILP {milp_status}: fallback greedy prioritas]{log_extra}"
                    else:
                        # Trip instan (commit optimistis), lalu validasi AC pasca-pemutusan (hanya menambah)
                        added, new_shed_set, recomputed = shed_commit(new_shed_set, current_tripped,
                                                                      effective_deficit, live_loads)
                        if added:
                            print(f"[SUPERVISOR TRIP UFLS] Breaker dieksekusi via MILP: {sorted(new_shed_set)}"
                                  f"{' (dihitung ulang: jalur cepat sudah bertindak)' if recomputed else ''}")
                        new_shed_set, log_extra = ac_validate_and_extend(
                            new_shed_set, effective_deficit, live_loads, gen_statuses, freq_hz, "UFLS")
                    shed_set = new_shed_set
                    log_msg = (f"UFLS TRIGGERED! Melepas {len(shed_set)} beban "
                               f"({shed_mw_of(shed_set, live_loads):.0f}MW): {', '.join(sorted(shed_set))}"
                               f"{stage_note}{log_extra}")
                    if len(shed_set) > len(current_tripped):
                        app.shed_holdoff = SHED_HOLDOFF_CYCLES

            # ── 4c. DEFISIT KAPASITAS (generator trip / demand > kapasitas)
            elif capacity_deficit > 0:
                effective_deficit = shed_target

                current_online_gens = {g['name'] for g in online_gens}
                gen_trip_occurred = current_online_gens != app.last_online_gens
                app.last_online_gens = current_online_gens
                # Generator baru hilang selalu menembus holdoff & dead-band (holdoff hanya untuk riak noise)
                gen_lost = app.gen_lost_pending

                if app.shed_holdoff > 0 and not gen_lost:
                    app.shed_holdoff -= 1
                    shed_set = current_tripped.copy()
                    log_msg = (f"DEFISIT TERTANGANI. Menahan kondisi pasca-pemotongan "
                               f"({app.shed_holdoff} siklus lagi, {shed_mw_of(shed_set, live_loads):.0f}MW).")

                # Dead-band histeresis 3.5 MW: jangan potong beban tambahan karena riak noise
                elif not (gen_trip_occurred or gen_lost) and currently_shed_mw >= (effective_deficit - 3.5) and freq_hz > 49.0:
                    shed_set = current_tripped.copy()
                    # Restorasi parsial: sebagian generator kembali -> target shedding turun. Beban kelebihan
                    # boleh dipulihkan bertahap selama sisa beban padam tetap >= target (terukur, lihat restoration_step)
                    surplus = currently_shed_mw - effective_deficit
                    smallest = min((l['mw'] for l in live_loads if l['name'] in shed_set), default=None)
                    # app.restoring: beban yang baru dipulihkan masih naik -> restoration_step tetap dipanggil (penjaga aktif)
                    if app.shed_holdoff == 0 and ((smallest is not None and surplus >= smallest) or app.restoring):
                        shed_set, log_msg = restoration_step(shed_set, live_loads, gen_statuses, freq_hz,
                                                             keep_mw=effective_deficit)
                    else:
                        app.restore_timer = 0
                        log_msg = (f"DEFISIT TERTANGANI. Mempertahankan {len(shed_set)} beban mati "
                                   f"({shed_mw_of(shed_set, live_loads):.0f}MW, target {effective_deficit:.0f}MW).")

                elif (gen_trip_occurred or gen_lost) and currently_shed_mw >= effective_deficit - SHED_TOL_MW:
                    # Generator baru hilang tetapi jalur cepat sudah melepas cukup: selisih kecil (beban bergeser sejak
                    # tabel pre-armed dihitung) tidak boleh menambah satu beban utuh (over-shedding)
                    app.gen_lost_pending = False
                    shed_set = current_tripped.copy()
                    print(f"[TARGET SHED] {effective_deficit:.1f} MW: sudah dilepas {currently_shed_mw:.1f} MW "
                          f"(selisih {effective_deficit - currently_shed_mw:+.1f} MW, toleransi {SHED_TOL_MW:.1f} MW) -> tidak ditambah")
                    log_msg = (f"DEFISIT TERTANGANI oleh jalur cepat: {len(shed_set)} beban mati "
                               f"({currently_shed_mw:.0f}MW, target {effective_deficit:.0f}MW).")

                elif currently_shed_mw < (effective_deficit - SHED_TOL_MW) or gen_trip_occurred or gen_lost:
                    app.gen_lost_pending = False
                    print(f"[TARGET SHED] {effective_deficit:.1f} MW ({shed_target_detail})")
                    new_shed_set, milp_status = solve_milp_shedding(effective_deficit, live_loads, current_tripped,
                                                                    freq_hz=freq_hz, droop_tolerance=0, keep_tripped=True,
                                                                    label="DEFISIT: generator trip / kapasitas kurang")
                    if milp_status == 'Optimal':
                        added, new_shed_set, recomputed = shed_commit(new_shed_set, current_tripped,
                                                                      effective_deficit, live_loads)
                        if added:
                            print(f"[SUPERVISOR TRIP GEN] Breaker dieksekusi via MILP: {sorted(new_shed_set)}"
                                  f"{' (dihitung ulang: jalur cepat sudah bertindak)' if recomputed else ''}")
                        new_shed_set, log_extra = ac_validate_and_extend(
                            new_shed_set, effective_deficit, live_loads, gen_statuses, freq_hz, "GEN")
                        shed_set = new_shed_set
                        log_msg = (f"DEFISIT TERDETEKSI! Melepas {len(shed_set)} beban "
                                   f"({shed_mw_of(shed_set, live_loads):.0f}MW): {', '.join(sorted(shed_set))}{log_extra}")
                        if len(new_shed_set) > len(current_tripped):
                            app.shed_holdoff = SHED_HOLDOFF_CYCLES
                    else:
                        new_shed_set = greedy_shed(effective_deficit, live_loads, current_tripped)
                        print(f"[GEN FALLBACK] MILP {milp_status}: greedy prioritas -> {sorted(new_shed_set)}")
                        shed_add(new_shed_set)
                        shed_set, log_extra = ac_validate_and_extend(
                            new_shed_set, effective_deficit, live_loads, gen_statuses, freq_hz, "GEN")
                        log_msg = (f"DEFISIT TERDETEKSI! MILP {milp_status}, fallback greedy prioritas: melepas "
                                   f"{len(shed_set)} beban ({shed_mw_of(shed_set, live_loads):.0f}MW){log_extra}")
                        if len(shed_set) > len(current_tripped):
                            app.shed_holdoff = SHED_HOLDOFF_CYCLES

                elif app.priority_changed:
                    # Operator mengubah prioritas di HMI -> evaluasi ulang konfigurasi trip
                    app.priority_changed = False
                    new_shed_set, milp_status = solve_milp_shedding(effective_deficit, live_loads, current_tripped,
                                                                    freq_hz=freq_hz, droop_tolerance=0)
                    if new_shed_set != current_tripped and milp_status == 'Optimal':
                        shed_replace(new_shed_set)   # aksi operator: satu-satunya jalur yang boleh menukar beban
                        new_shed_set, log_extra = ac_validate_and_extend(
                            new_shed_set, effective_deficit, live_loads, gen_statuses, freq_hz)
                        shed_set = new_shed_set
                        log_msg = (f"RE-PRIORITIZED! Menukar beban mati sesuai prioritas operator "
                                   f"({shed_mw_of(shed_set, live_loads):.0f}MW).{log_extra}")
                        if len(new_shed_set) > len(current_tripped):
                            app.shed_holdoff = SHED_HOLDOFF_CYCLES
                    else:
                        shed_set = current_tripped.copy()

                else:
                    # State lock: shedding sudah cukup, cegah hunting
                    shed_set = current_tripped.copy()

            # ── 4d. KAPASITAS CUKUP: feeder contingency / topologi / restorasi
            else:
                shed_set = current_tripped.copy()

                if app.shed_holdoff > 0:
                    # Holdoff dicek dulu agar SOCP tidak re-trigger dan beban tidak mati-nyala
                    app.shed_holdoff -= 1
                    app.restore_timer = 0
                    app.restore_cooldown = 0
                    log_msg = f"RESTORASI DITAHAN: Cooldown pasca-shedding ({app.shed_holdoff} siklus lagi)."

                elif STATIC_SCHEME and (app.feeder_contingency
                                        or not ac_check_cached(gen_statuses, live_loads, current_tripped)[0]):
                    app.restore_timer = 0
                    log_msg = ("UFLS STATIS: pelanggaran tegangan/loading tidak ditangani (skema konvensional "
                               "hanya merespons frekuensi). Tahan state.")

                elif app.feeder_contingency:
                    # Feeder masih offline: shed awal sekali, lalu tahan dan tunggu feeder pulih
                    is_valid_fc, viol_buses = ac_check_cached(gen_statuses, live_loads, current_tripped)
                    if not is_valid_fc:
                        if len(current_tripped) == 0:
                            shed_set_fc, reason = solve_feeder_contingency_shedding(
                                gen_statuses, live_loads, current_tripped, app.line_statuses, viol_buses)
                            if shed_set_fc:
                                shed_set = shed_set_fc
                                app.feeder_shed |= set(shed_set_fc) - current_tripped
                                log_msg = (f"FEEDER TRIP [{reason}]: Shedding {len(shed_set)} beban "
                                           f"({shed_mw_of(shed_set, live_loads):.0f}MW) untuk amankan tegangan & saluran.")
                                app.shed_holdoff = SHED_HOLDOFF_CYCLES
                            else:
                                log_msg = "FEEDER TRIP: Gagal menemukan skema shedding aman, tahan state."
                        else:
                            app.restore_timer = 0
                            log_msg = "FEEDER CONTINGENCY: Jaringan terkendala (overload gen/topologi). Tahan state, tunggu feeder pulih."
                    else:
                        # AC valid: tunggu frekuensi stabil; restorasi baru setelah feeder kembali
                        if 49.5 <= freq_hz <= 50.5:
                            app.feeder_stable_count += 1
                        else:
                            app.feeder_stable_count = 0
                            app.restore_timer = 0
                            log_msg = f"FEEDER CONTINGENCY: Menunggu frekuensi stabil ({freq_hz:.2f}Hz, perlu 49.5-50.5Hz)."
                        if app.feeder_stable_count >= 5:
                            log_msg = f"FEEDER CONTINGENCY: Frekuensi stabil ({freq_hz:.2f}Hz). Tahan beban — tunggu feeder pulih sebelum restore."
                        elif app.feeder_stable_count > 0:
                            log_msg = f"FEEDER CONTINGENCY: Menunggu stabilitas ({app.feeder_stable_count}/5 siklus stabil)."

                else:
                    # Mode normal: proactive topology check, lalu restorasi bertahap
                    is_valid, viol_buses = ac_check_cached(gen_statuses, live_loads, current_tripped)
                    if not is_valid:
                        shed_set_targeted, reason = solve_feeder_contingency_shedding(
                            gen_statuses, live_loads, current_tripped, app.line_statuses, viol_buses)
                        if shed_set_targeted:
                            shed_set = shed_set_targeted
                            app.feeder_shed |= set(shed_set_targeted) - current_tripped
                            log_msg = (f"TOPOLOGY TRIGGER [{reason}]! Melepas {len(shed_set)} beban "
                                       f"({shed_mw_of(shed_set, live_loads):.0f}MW) untuk menyelamatkan tegangan / overload.")
                            if len(shed_set) > len(current_tripped):
                                app.shed_holdoff = SHED_HOLDOFF_CYCLES
                        else:
                            log_msg = "TOPOLOGY TRIGGER: Gagal menemukan solusi shedding, mempertahankan state."

                    elif len(shed_set) == 0 and not app.restoring:
                        # (beban terakhir yang baru dipulihkan masih naik -> lewat restoration_step: penjaga tetap aktif)
                        app.restore_timer = 0
                        app.restore_cooldown = 0
                        app.restoring = None
                        log_msg = "SISTEM NORMAL: Seluruh beban beroperasi penuh (100% tersuplai)."

                    else:
                        shed_set, log_msg = restoration_step(shed_set, live_loads, gen_statuses, freq_hz)

            cycle_meter.mark('keputusan')

            # ── 5. Input tabel mask pre-armed (dihitung thread arming; entri N-1 juga tampil di HMI)
            app.arm_inputs = ([dict(g) for g in gen_statuses], [dict(l) for l in live_loads],
                              dict(app.line_statuses), dict(app.gen_memory), feeder_mw)
            arm_key = (frozenset(g['name'] for g in online_gens), frozenset(shed_snapshot() | shed_set),
                       tuple(sorted(app.line_statuses.items())), tuple(l['priority'] for l in LOADS))
            if arm_key != app.arm_key:
                app.arm_key = arm_key
                arm_event.set()
            with arm_lock:
                table = app.arm['table']
            app.contingency_matrix = {g['name']: sorted(table.get((g['name'],), ())) for g in gen_statuses}
            cycle_meter.mark('matriks_n1')

            # ── 6. Komit penambahan dari cabang 4d (feeder/topologi) lalu tulis cmd_shed TERBARU ke PLC
            #       (tidak pernah menghapus trip dari jalur cepat; penghapusan hanya lewat restorasi/operator)
            with shed_lock:
                cmd_shed.update(shed_set)
            shed_set = _write_breakers('SUPERVISOR', bits)
            load_statuses = [{'id': l['name'], 'status': 'TRIPPED' if l['name'] in shed_set else 'NORMAL',
                              'mw': l['actual_mw'], 'priority': l['priority'],
                              'default_priority': l.get('default_priority', 2)} for l in live_loads]

            fb_msg, fb_info = evaluate_plc_feedback(load_statuses, fb_bits)
            while timing_log_queue:
                t_msg = timing_log_queue.pop(0)
                fb_msg = f"{fb_msg} | {t_msg}" if fb_msg else t_msg
            if fb_msg:
                log_msg = fb_msg if log_msg.startswith("Sistem Stabil") else f"{log_msg} | {fb_msg}"
            cycle_meter.mark('kirim_feedback')

            # ── 7. Live load flow (saat status breaker berubah atau tiap 30 siklus)
            app.lf_timer += 1
            breaker_state = "-".join([g['status'] for g in gen_statuses] + [l['status'] for l in load_statuses])
            if app.last_breaker_state != breaker_state or app.lf_timer >= 30:
                app.last_breaker_state = breaker_state
                app.lf_timer = 0
                threading.Thread(target=_run_live_loadflow_bg, name='loadflow',
                                 args=(copy.deepcopy(gen_statuses), copy.deepcopy(live_loads),
                                       set(shed_set), dict(app.line_statuses)),
                                 daemon=True).start()

            # ── 8. Log & broadcast ke HMI
            current_active_load = sum(l['actual_mw'] for l in live_loads)
            now_m = time.monotonic()
            if (now_m - app.status_print_t >= STATUS_PRINT_S or abs(freq_hz - 50.0) >= 0.1
                    or breaker_state != app.status_print_state):
                print(f"| GEN:{total_gen:.0f}MW f={freq_hz:.2f}Hz "
                      f"LOAD:{current_active_load:.0f}MW DEF:{max(0, capacity_deficit):.0f}MW | {plc_time} |")
                app.status_print_t, app.status_print_state = now_m, breaker_state

            socketio.emit('grid_update', {
                'total_gen':   total_gen,
                'total_load':  current_active_load,
                'deficit':     max(0, capacity_deficit),
                'frequency':   round(freq_hz, 3),
                'generators':  gen_statuses,
                'sensor_gens': [{'name': n, 'mw': gen_mw[n]} for n in GEN_ORDER],
                # HMI menampilkan status hasil feedback PLC; 'cmd_status' = keputusan MILP
                'loads':       [{**ls, 'status': ls.get('plc_status', ls['status']), 'cmd_status': ls['status']}
                                for ls in load_statuses],
                'lines':       app.line_statuses,
                'plc_time':    plc_time,
                'contingency': app.contingency_matrix,
                'feedback':    fb_info,
                'gen_panel':   app.gen_panel,
                'log':         f"[{time.strftime('%H:%M:%S')}] {log_msg}",
            })
            cycle_meter.mark('emit')

        except Exception as e:
            app.plc_data_valid = False
            print(f"[ERROR Monitor] {e}")

        cycle_meter.work_done()
        time.sleep(0.1)
        cycle_meter.end()


# =========================================================
# JALUR CEPAT PRE-ARMED (tabel mask MILP + trip seketika saat generator lepas)
# =========================================================
def build_arm_table(gen_statuses, live_loads, line_statuses, gen_memory, feeder_mw=0.0):
    """Tabel mask: (A,) -> beban lepas bila A trip; (A, B) -> bila B trip setelah A (B >= mask(A)).
    Target sama dengan supervisor (shed_requirement); beban yang sudah padam wajib tetap padam.
    Mask N-1 divalidasi AC (hanya menambah); mask N-2 diperiksa supervisor pasca-kejadian."""
    base = shed_snapshot()
    online = [g['name'] for g in gen_statuses if g['status'] == 'ONLINE']
    offline = [g['name'] for g in gen_statuses if g['status'] != 'ONLINE']
    rated = {g['name']: g['rated'] for g in gen_statuses}
    mem = lambda n: gen_memory.get(n, rated[n] * 0.9)
    demand = sum(l['mw'] for l in live_loads)
    lost0 = sum(mem(n) for n in offline)
    cap0 = sum(rated[n] for n in online)

    def solve(lost_names, keep):
        lost = lost0 + sum(mem(n) for n in lost_names)
        cap = cap0 - sum(rated[n] for n in lost_names)
        target, _ = shed_requirement(lost, demand, cap, feeder_mw)
        if shed_mw_of(keep, live_loads) >= target:
            return set(keep), target
        res, status = solve_milp_shedding(target, live_loads, keep, droop_tolerance=0, keep_tripped=True)
        return (set(res) if status == 'Optimal' else greedy_shed(target, live_loads, keep)), target

    table, targets = {}, {}
    for a in online:
        mask_a, target_a = solve([a], base)
        gs_a = [{**g, 'status': 'OFFLINE', 'mw': 0.0} if g['name'] == a else g for g in gen_statuses]
        mask_a, _ = ac_extend(mask_a, target_a, live_loads, gs_a, line_statuses)
        table[(a,)], targets[(a,)] = frozenset(mask_a), target_a
        for b in online:
            if b != a:
                mask_ab, targets[(a, b)] = solve([a, b], mask_a)
                table[(a, b)] = frozenset(mask_ab)
    return {'table': table, 'targets': targets, 'base_online': frozenset(online),
            'base_shed': frozenset(base), 'live_loads': live_loads}


def arming_worker():
    """Hitung ulang tabel mask tiap ARM_REFRESH_S, atau segera bila kondisi sistem berubah."""
    print("--- Arming tabel mask pre-armed dimulai ---")
    while True:
        arm_event.wait(timeout=ARM_REFRESH_S)
        arm_event.clear()
        inputs = app.arm_inputs
        if inputs is None:
            continue
        t_inputs, t0 = time.monotonic(), time.perf_counter()
        try:
            arm = build_arm_table(*inputs)
        except Exception as e:
            print(f"[ARM] Gagal menghitung tabel mask: {e}")
            continue
        arm.update(t=t_inputs, calc_ms=(time.perf_counter() - t0) * 1000.0)
        with arm_lock:
            app.arm = arm


def fast_trip_watch():
    """Baca status generator (%MW10000 = bit %M0-%M15) tiap FAST_POLL_S. Saat generator ONLINE->OFFLINE,
    kirim mask pre-armed seketika (tanpa MILP/AC di jalur ini). Supervisor tetap mengoreksi sesudahnya."""
    print(f"--- Jalur cepat pre-armed dimulai (poll {FAST_POLL_S * 1000:.0f} ms) ---")
    prev, event_gens, event_t = None, [], 0.0
    while True:
        time.sleep(FAST_POLL_S)
        try:
            if not app.plc_data_valid:
                prev = None                        # data PLC belum sah: acuan status diambil ulang sesudahnya
                continue
            t_seen, word = app.gen_word_seen
            if time.perf_counter() - t_seen >= FAST_POLL_S:
                # Loop utama belum membaca word ini dalam FAST_POLL_S terakhir -> poll sendiri
                with plc_lock:
                    r = client.read_holding_registers(address=COIL_BASE_MW, count=1)
                if r.isError():
                    continue                       # gagal baca BUKAN trip
                t_seen = time.perf_counter()
                word = r.registers[0]
            online = {g for g in GEN_ORDER if (word >> GEN_MAP[g]['coil_read']) & 1}
            if prev is None:
                prev = online
                continue
            lost = [g for g in GEN_ORDER if g in prev and g not in online]
            prev = online
            if not lost:
                continue
            now = time.monotonic()
            if not event_gens or now - event_t > GEN_MEMORY_FREEZE_S:
                event_gens, event_t = [], now
            lost.sort(key=lambda g: -app.gen_memory.get(g, 0.0))   # simultan: unit terbesar dulu
            event_gens += lost
            timing_on_gen_offline(lost)
            with arm_lock:
                arm = app.arm
            age = now - arm['t']
            key = tuple(g for g in event_gens if g in arm['base_online'])
            mask = arm['table'].get(key)
            if not key or mask is None or age > ARM_MAX_AGE_S:
                print(f"[FAST TRIP] {'+'.join(event_gens)}: tidak ada mask (kunci {key}, umur tabel {age:.1f}s)"
                      f" -> diserahkan ke supervisor")
                continue
            # Commit optimistis: mask dihitung dari base (cmd_shed saat tabel dihitung; untuk (A, B) = mask(A)).
            # Bila cmd_shed sudah berubah (beban feeder / supervisor sudah bertindak), evaluasi ulang
            # terhadap kondisi terbaru agar tidak melepas beban yang tidak diperlukan lagi.
            base = arm['base_shed'] if len(key) == 1 else arm['table'][key[:1]]
            new, mask, recomputed = shed_commit(mask, base, arm['targets'][key], arm['live_loads'], 'FAST')
            note = " (dievaluasi ulang: beban padam sudah berubah)" if recomputed else ""
            print(f"[FAST TRIP] {'+'.join(event_gens)} -> {sorted(mask)} | baru: {sorted(new)} | "
                  f"deteksi->PLC {(time.perf_counter() - t_seen) * 1000:.0f} ms | umur tabel {age:.1f}s "
                  f"(hitung {arm['calc_ms']:.0f} ms){note}")
            arm_event.set()
        except Exception as e:
            prev = None                            # koneksi putus: jangan bandingkan dengan status sebelum putus
            print(f"[FAST TRIP] Error: {e}")


# =========================================================
# BASELINE PEMBANDING: UFLS STATIS (env SCHEME=ufls_static)
# =========================================================
# Skema konvensional: relai frekuensi bertahap dengan blok beban TETAP, disusun sekali dari prioritas default
# dan daya nominal (bukan dari kondisi saat gangguan). Setting di bawah ILUSTRATIF (default), BUKAN setting
# acuan utilitas: 5 tahap 49.5/49.3/49.1/48.9/48.7 Hz, tiap blok >= 10% beban nominal total (P2 lalu P3,
# urutan daftar LOADS; P4 kritis tidak masuk UFLS), tunda relai 0.1 s, frekuensi %MW54 dibaca tiap FAST_POLL_S.
# Tanpa jalur cepat, tanpa MILP/validasi AC, tanpa pelepasan karena beban lebih feeder.
# Restorasi memakai logika yang sama dengan skema optimal (restoration_step).
SCHEME = os.getenv('SCHEME', 'optimal').lower()
STATIC_SCHEME = (SCHEME == 'ufls_static')
UFLS_STATIC_STAGES_HZ = (49.5, 49.3, 49.1, 48.9, 48.7)
UFLS_STATIC_BLOCK_FRAC = 0.10
UFLS_STATIC_DELAY_S = 0.10


def build_static_blocks():
    """Blok beban tetap per tahap: isi berurutan sampai >= UFLS_STATIC_BLOCK_FRAC x beban nominal total."""
    total = sum(l['max_mw'] for l in LOADS)
    order = [l for l in sorted(LOADS, key=lambda x: x['default_priority']) if l['default_priority'] < 4]
    blocks, cur = [], []
    for l in order:
        cur.append(l['name'])
        if sum(x['max_mw'] for x in LOADS if x['name'] in cur) >= UFLS_STATIC_BLOCK_FRAC * total:
            blocks.append(cur)
            cur = []
        if len(blocks) == len(UFLS_STATIC_STAGES_HZ):
            break
    if cur and len(blocks) < len(UFLS_STATIC_STAGES_HZ):
        blocks.append(cur)
    return blocks


def ufls_static_watch():
    """Relai UFLS statis: f <= ambang tahap selama UFLS_STATIC_DELAY_S -> lepas blok tahap itu (monoton)."""
    blocks = build_static_blocks()
    stages = list(zip(UFLS_STATIC_STAGES_HZ, blocks))
    print("--- BASELINE UFLS STATIS (setting ilustratif): "
          + " | ".join(f"{hz:.1f}Hz: {'+'.join(b)}" for hz, b in stages) + f" | tunda {UFLS_STATIC_DELAY_S:.2f}s ---")
    below = [None] * len(stages)
    while True:
        time.sleep(FAST_POLL_S)
        try:
            with plc_lock:
                r = client.read_holding_registers(address=ADDR_FREQ, count=1)
            if r.isError() or r.registers[0] == 0:
                continue
            f = r.registers[0] / 100.0
            now = time.perf_counter()
            for i, (hz, block) in enumerate(stages):
                if f > hz:
                    below[i] = None
                    continue
                below[i] = below[i] or now
                if now - below[i] >= UFLS_STATIC_DELAY_S:
                    new = shed_add(block, 'UFLS_STATIS')
                    if new:
                        print(f"[UFLS STATIS] Tahap {i + 1} ({hz:.1f} Hz) bekerja pada f={f:.2f}Hz -> {sorted(new)}")
        except Exception as e:
            print(f"[UFLS STATIS] Error: {e}")
            time.sleep(1.0)


def start_background_threads():
    threading.Thread(target=background_monitoring, daemon=True, name='loop_utama').start()
    if MODBUS_DIAG:
        threading.Thread(target=modbus_diag_worker, daemon=True, name='diag').start()
    if STATIC_SCHEME:
        threading.Thread(target=ufls_static_watch, daemon=True, name='ufls_statis').start()
        print("--- SKEMA PEMBANDING: UFLS statis (jalur cepat, MILP, dan arming nonaktif) ---")
        return
    threading.Thread(target=arming_worker, daemon=True, name='arming').start()
    if FAST_TRIP_ENABLED:
        threading.Thread(target=fast_trip_watch, daemon=True, name='jalur_cepat').start()
    else:
        print("--- Jalur cepat NONAKTIF (FAST_TRIP=0): hanya supervisor ---")


def _run_live_loadflow_bg(gen_statuses, live_loads, tripped, line_statuses):
    try:
        res = run_live_loadflow(gen_statuses, live_loads, tripped, line_statuses)
        socketio.emit('live_loadflow_result', {'status': 'success', 'data': res})
    except Exception as e:
        socketio.emit('live_loadflow_result', {'status': 'error', 'message': str(e)})
        print(f"[SCADA] Error Live Load Flow: {e}")


# =========================================================
# ROUTES
# =========================================================
@app.route('/')
def index():
    return render_template('index.html', role=request.args.get('role', 'viewer'))


# =========================================================
# SOCKET - KONTROL GENERATOR & SALURAN
# =========================================================
def handle_gen_switches(bits):
    """Panel tombol fisik generator (bit %M72-75 = SW_Gen1-4).
    - Perubahan posisi tombol -> handle_gen_control (jalur yang sama dengan klik HMI: log, peringatan
      sinkronisasi, jalur cepat, pengukuran waktu shedding).
    - Saat start / setelah data PLC tidak sah: posisi hanya dicatat sebagai acuan, tanpa aksi.
    - Penjaga: OFF hanya dikirim bila generator belum di-trip, ON hanya bila sedang di-trip (bit perintah
      %M50-53 siklus ini). Mencegah perintah ganda dan gen_cmd_time basi (pengukuran waktu palsu).
    - Relay penahan tidak bisa dilepas dari HMI (tidak ada output PLC tersisa), jadi posisi tombol bisa
      berbeda dari perintah generator bila HMI dipakai. Bila berbeda >= SWITCH_MISMATCH_WARN_S: peringatan
      sekali di log HMI + badge PANEL kuning."""
    if not PANEL_BUTTONS_ENABLED:
        app.gen_panel = None
        return
    now_sw = {name: bool(bits[b]) for b, name in GEN_SWITCH_BITS.items()}
    prev = app.gen_switch_prev
    app.gen_switch_prev = now_sw
    if prev is None:
        app.gen_switch_mismatch.clear()
        app.gen_switch_warned.clear()
    else:
        for name in GEN_ORDER:
            if now_sw[name] == prev[name]:
                continue
            action = 'ON' if now_sw[name] else 'OFF'
            tripped = bool(bits[GEN_MAP[name]['coil_write']])
            if (action == 'OFF') == tripped:
                print(f"[TOMBOL FISIK] {name} {action} diabaikan (generator sudah {'OFF' if tripped else 'ON'})")
                continue
            print(f"[TOMBOL FISIK] {name} {action}")
            socketio.emit('interrupt_ack', {'type': 'gen', 'message': f"TOMBOL FISIK: {name} {action}"})
            handle_gen_control({'name': name, 'action': action})
    # Kesesuaian posisi tombol vs perintah generator (bit %M50-53 dibaca siklus ini)
    t = time.monotonic()
    panel = {}
    for name in GEN_ORDER:
        cmd_on = not bits[GEN_MAP[name]['coil_write']]
        mismatch = False
        if now_sw[name] != cmd_on:
            since = app.gen_switch_mismatch.setdefault(name, t)
            mismatch = t - since >= SWITCH_MISMATCH_WARN_S
            if mismatch and name not in app.gen_switch_warned:
                app.gen_switch_warned.add(name)
                hint = 'tekan MERAH' if now_sw[name] else 'tekan HIJAU'
                msg = (f"PANEL: tombol {name} posisi {'ON' if now_sw[name] else 'OFF'}, generator "
                       f"{'ON' if cmd_on else 'OFF'} -> {hint} untuk menyamakan.")
                print(f"[TOMBOL FISIK] {msg}")
                socketio.emit('interrupt_ack', {'type': 'gen', 'message': msg})
        else:
            app.gen_switch_mismatch.pop(name, None)
            app.gen_switch_warned.discard(name)
        panel[name] = {'sw': now_sw[name], 'mismatch': mismatch}
    app.gen_panel = panel


@socketio.on('gen_control')
def handle_gen_control(data):
    """ON/OFF breaker generator (bit perintah %M50-%M53)."""
    name, action = data.get('name'), data.get('action')
    if name in GEN_MAP:
        if action == 'OFF':
            gen_cmd_time[name] = time.perf_counter()   # awal pengukuran waktu shedding
        elif action == 'ON' and last_live_loads and app.arm_inputs:
            # Peringatan (tidak memblokir): unit masuk di output minimum, sedangkan unit lain hanya bisa turun
            # sebatas ramp (x3 runback) selama sinkronisasi (+-SYNC_WINDOW_S) dan tidak di bawah minimum.
            # Bila generasi yang tak bisa dihindari > beban -> frekuensi naik (restorasi korektif akan aktif).
            gen_now = [g for g in app.arm_inputs[0] if g['status'] == 'ONLINE' and g['name'] != name]
            floor = GEN_MAP[name]['min'] + sum(
                g['mw'] - min(g['mw'] - GEN_MAP[g['name']]['min'], GEN_MAP[g['name']]['ramp'] * 3.0 * SYNC_WINDOW_S)
                for g in gen_now)
            load_now = sum(l['actual_mw'] for l in last_live_loads)
            if floor > load_now:
                msg = (f"PERINGATAN SINKRONISASI {name}: generasi minimum yang tak terhindarkan {floor:.0f}MW > beban "
                       f"{load_now:.0f}MW -> risiko frekuensi lebih; restorasi korektif akan aktif.")
                print(f"[SINKRON] {msg}")
                socketio.emit('interrupt_ack', {'type': 'gen', 'message': msg})
        modbus_watch_start()
        with plc_lock:
            client.write_coil(GEN_MAP[name]['coil_write'], value=(action == 'OFF'))
        print(f">>> Command Web: {action} {name}")


@socketio.on('toggle_line')
def handle_toggle_line(data):
    """ON/OFF saluran transmisi (feeder contingency)."""
    name, action = data.get('name'), data.get('action')
    if name in app.line_statuses:
        modbus_watch_start()
        app.line_statuses[name] = 'ONLINE' if action == 'ON' else 'OFFLINE'
        app.last_breaker_state = "LINE_TOGGLE_TRIGGER"   # paksa live load flow segera
        print(f">>> SALURAN TRANSMISI {'DIPUTUS' if action == 'OFF' else 'DISAMBUNG'}: {name}")


# =========================================================
# SOCKET - OVERRIDE PER-CHANNEL BEBAN KE PLC
# =========================================================
@socketio.on('set_load_interrupt')
def handle_set_load_interrupt(data):
    """
    Override per-channel nilai beban dari HMI.
    data = {'loads': [L101, L201, L303, L304, L305, L401, L301, L302, L402, L403, L404, L405]}
    0 = AUTO (load.py menghitung sendiri), >0 = channel dipaksa ke nilai ini.
    Ditulis ke %MW40-51 (dibaca load.py) dan langsung ke %MW0-11 untuk efek instan.
    """
    try:
        vals = [max(0, min(32767, int(round(float(v) * 10)))) for v in data.get('loads', [0] * 12)]
        if len(vals) != 12:
            print("[OVERRIDE BEBAN] Data tidak valid — harus 12 nilai.")
            return
        with plc_lock:
            client.write_registers(address=ADDR_OVERRIDE, values=vals)
            for i, v in enumerate(vals):
                if v > 0:
                    client.write_register(address=ADDR_SENSOR_LOAD + i, value=v)

        n_config = sum(1 for v in vals if v > 0)
        print(f"[OVERRIDE BEBAN] Config: { {f'%MW{40 + i}={v / 10.0}' for i, v in enumerate(vals) if v > 0} }")
        if n_config < 12:
            print(f"[OVERRIDE BEBAN] Auto: {12 - n_config} channel")
        socketio.emit('interrupt_ack', {
            'type': 'load',
            'message': f"Override beban dikirim. {n_config} channel CONFIG, {12 - n_config} channel AUTO.",
            'values': vals,
        })
    except Exception as e:
        print(f"[OVERRIDE BEBAN] Error: {e}")


# =========================================================
# SOCKET - INJECT NILAI GENERASI (MW) KE PLC
# =========================================================
@socketio.on('set_gen_interrupt')
def handle_set_gen_interrupt(data):
    """Inject nilai generasi dari HMI ke %MW30-33. data = {'gens': [PLTA, PLTS, PLTGU, PLTB]}"""
    try:
        vals = [max(0, min(32767, int(round(float(v) * 10)))) for v in data.get('gens', [0, 0, 0, 0])]   # MW x10
        if len(vals) != 4:
            print("[INJECT GEN] Data tidak valid — harus 4 nilai.")
            return
        with plc_lock:
            client.write_registers(address=ADDR_SENSOR_GEN, values=vals)
        info = ', '.join(f"{name}={v / 10.0}MW" for name, v in zip(GEN_ORDER, vals))
        print(f"[INJECT GEN] %MW30–%MW33: {info}")
        socketio.emit('interrupt_ack', {'type': 'gen', 'message': f"Generasi dikirim ke PLC: {info}", 'values': vals})
    except Exception as e:
        print(f"[INJECT GEN] Error: {e}")


# =========================================================
# SOCKET - UPDATE PRIORITAS BEBAN (BOBOT MILP)
# =========================================================
def persist_priority_to_file(load_name, new_prio):
    """Simpan prioritas baru ke daftar LOADS di file ini agar bertahan setelah restart."""
    try:
        with open(__file__, 'r', encoding='utf-8') as f:
            content = f.read()
        pattern = r"(\{'name':\s*'" + load_name + r"',\s*'priority':\s*)\d+"
        with open(__file__, 'w', encoding='utf-8') as f:
            f.write(re.sub(pattern, r"\g<1>" + str(new_prio), content))
        print(f"[PERSIST] {load_name} priority updated to {new_prio} in app.py")
    except Exception as e:
        print(f"[PERSIST] Failed to rewrite app.py: {e}")


@socketio.on('set_load_priority')
def handle_set_load_priority(data):
    """
    Update prioritas MILP beban. 2 = Low (non-esensial), 3 = Medium (esensial), 4 = High (kritis).
    data = {'load': 'L101', 'priority': 3}
    """
    load_id = data.get('load')
    priority = max(2, min(4, int(data.get('priority', 2))))

    load = next((l for l in LOADS if l['name'] == load_id), None)
    if load is None:
        print(f"[PRIORITY] Load '{load_id}' tidak ditemukan.")
        return
    if load['priority'] == priority:
        return   # tidak ada perubahan

    print(f"[PRIORITY] {load_id}: {load['priority']} → {priority}")
    load['priority'] = priority
    app.priority_changed = True
    persist_priority_to_file(load_id, priority)

    label = {2: 'LOW (Non-Esensial)', 3: 'MEDIUM (Esensial)', 4: 'HIGH (Kritis)'}[priority]
    socketio.emit('interrupt_ack', {
        'type': 'priority',
        'message': f"Priority {load_id} diperbarui → {label}",
        'load': load_id,
        'priority': priority,
    })


# =========================================================
# PROGRAM UTAMA
# =========================================================
if __name__ == '__main__':
    print("Mempersiapkan Sistem SCADA...")
    print(f"PLC Target: {PLC_IP}:{PORT}")
    print("Alamat MW:")
    print("  %MW0--%MW11  -> Nilai beban aktual (load.py tulis tiap siklus)")
    print("  %MW20--%MW21 -> Jam simulasi")
    print("  %MW30--%MW33 -> Sensor Generasi PLTA/PLTS/PLTGU/PLTB")
    print("  %MW40--%MW51 -> Override per-channel HMI (0=AUTO, >0=CONFIG/FORCE)")
    print(f"Jalur cepat pre-armed: {'AKTIF' if FAST_TRIP_ENABLED else 'NONAKTIF'} (env FAST_TRIP=0 untuk menonaktifkan)")

    start_background_threads()

    print("Memulai Web Server di Port 5000...")
    socketio.run(app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)
