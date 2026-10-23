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
from plc_modbus import M241Client as ModbusTcpClient

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
BIT_BLOCK_COUNT  = 70

# =========================================================
# DATA GENERATOR & BEBAN
# =========================================================
# coil_read = status ONLINE (ditulis load.py), coil_write = perintah trip dari HMI
GEN_MAP = {
    'PLTA':  {'coil_read': 0, 'coil_write': 50, 'mw_addr': 30, 'type': 'hydro',      'rated': 75},
    'PLTS':  {'coil_read': 1, 'coil_write': 51, 'mw_addr': 31, 'type': 'solar_bess', 'rated': 25},
    'PLTGU': {'coil_read': 2, 'coil_write': 52, 'mw_addr': 32, 'type': 'gas',        'rated': 75},
    'PLTB':  {'coil_read': 3, 'coil_write': 53, 'mw_addr': 33, 'type': 'wind',       'rated': 25},
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
RESTORE_STABLE_CYCLES   = 30    # 30 siklus frekuensi stabil sebelum restorasi
RESTORE_COOLDOWN_CYCLES = 40    # jeda antar penyalaan beban
CONTINGENCY_REFRESH     = 10    # matriks N-1 dihitung ulang tiap 10 siklus (tampilan saja)

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
app.last_load_statuses  = []
app.last_online_gens    = set()
app.shed_holdoff        = 0
app.restore_timer       = 0
app.restore_cooldown    = 0
app.feeder_contingency  = False
app.feeder_stable_count = 0
app.lf_timer            = 0
app.last_breaker_state  = ''
app.contingency_matrix  = {}
app.contingency_key     = None
app.contingency_counter = CONTINGENCY_REFRESH

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


def timing_on_breaker_command(target_shed_set):
    """Dipanggil setiap perintah trip beban dikirim; tahap baru bila ada beban tambahan dilepas."""
    global last_cmd_shed
    newly_shed = set(target_shed_set) - last_cmd_shed
    last_cmd_shed = set(target_shed_set)
    event = shed_event
    if not newly_shed or event is None or time.perf_counter() - event['t_last'] > TIMING_EVENT_WINDOW_S:
        return
    event['stages'] += 1
    stage = {'n': event['stages'], 't_trip': time.perf_counter(), 'shed': set(target_shed_set)}
    event['t_last'] = stage['t_trip']
    threading.Thread(target=_wait_plc_feedback, args=(event, stage), daemon=True).start()


def _feedback_matches(fb_bits, expected_shed):
    for name, idx in FEEDBACK_INDEX.items():
        if bool(fb_bits[idx]) == (name in expected_shed):   # READ_LOAD=1 berarti beban ON
            return False
    return bool(fb_bits[CRITICAL_GROUP_INDEX]) == (not any(n in expected_shed for n in CRITICAL_GROUP))


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
        time.sleep(0.005)
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
    fmt = lambda v: f"{v:.0f} ms" if v is not None else "-"
    if stage['t_fb'] is None:
        msg = f"WAKTU SHEDDING {gens} tahap {stage['n']}: feedback PLC tidak terkonfirmasi dalam 3 s (cek PLC RUN)."
    elif stage['n'] == 1:
        msg = (f"WAKTU SHEDDING {gens} tahap 1: deteksi {fmt(detect)} + MILP/kirim {fmt(decide)} "
               f"+ PLC/feedback {fmt(plc_fb)} = total {fmt(total)} [{shed}]")
    else:
        msg = f"WAKTU SHEDDING {gens} tahap {stage['n']}: PLC/feedback {fmt(plc_fb)}, total sejak trip {fmt(total)} [{shed}]"
    print(f"[TIMING] {msg}")
    timing_log_queue.append(msg)
    try:
        new_file = not os.path.exists(TIMING_CSV)
        with open(TIMING_CSV, 'a', encoding='utf-8') as f:
            if new_file:
                f.write("waktu,generator,tahap,deteksi_ms,milp_kirim_ms,plc_feedback_ms,total_ms,beban_dilepas\n")
            vals = [f"{v:.1f}" if v is not None else "" for v in (detect, decide, plc_fb, total)]
            f.write(",".join([event['wall'], gens, str(stage['n'])] + vals + [shed]) + "\n")
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


def apply_breaker_shed_set(target_shed_set, live_loads):
    """Kirim status breaker beban ke PLC (satu batch, hanya word yang berubah)."""
    with plc_lock:
        client.write_coil_map({l['coil_write']: l['name'] in target_shed_set for l in live_loads})
    timing_on_breaker_command(target_shed_set)


def shed_mw_of(shed_set, live_loads):
    return sum(l['mw'] for l in live_loads if l['name'] in shed_set)


def ac_validate_and_swap(new_shed_set, calc_deficit, live_loads, current_tripped,
                         gen_statuses, freq_hz, milp_swap_note, print_tag=None):
    """Validasi AC pasca-shedding; jika melanggar, tukar beban (MILP bus sensitivity
    atau SOCP) dan kirim ke PLC. Return: (shed_set_final, catatan_log)."""
    is_valid, viol_buses = verify_ac_post_shedding(gen_statuses, live_loads, new_shed_set, app.line_statuses)
    if is_valid:
        return new_shed_set, ""

    if len(viol_buses) <= 2 and "ALL_BUSES" not in viol_buses:
        adjusted, _ = solve_milp_shedding(calc_deficit, live_loads, current_tripped,
                                          must_shed_loads=loads_on_buses(viol_buses), freq_hz=freq_hz)
        note = milp_swap_note
    else:
        socp_set, socp_status = solve_socp_fallback(gen_statuses, live_loads, current_tripped, app.line_statuses)
        adjusted = socp_set if socp_status in ("optimal", "optimal_inaccurate") else new_shed_set
        note = " [Post-AC Swap: Fallback SOCP]"

    if adjusted != new_shed_set:
        apply_breaker_shed_set(adjusted, live_loads)
        if print_tag:
            print(f"[AC SWAP {print_tag}] Breaker disesuaikan pasca-verifikasi AC: {sorted(adjusted)}")
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


# =========================================================
# SOCKET - DETEKSI KONEKSI
# =========================================================
@socketio.on('connect')
def handle_connect():
    print(">>> WEB BROWSER TERHUBUNG KE SERVER <<<")


# =========================================================
# BACKGROUND MONITORING
# =========================================================
def background_monitoring():
    global last_live_loads, prev_gen_online
    print("--- Background Monitoring Dimulai ---")
    while True:
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
            fb_bits = bits[FEEDBACK_COIL_BASE:FEEDBACK_COIL_BASE + 10] if bits else None

            # Generator: daya aktual %MW30-33 + status %M0-3
            gen_mw = {name: (mw[ADDR_SENSOR_GEN + i] / 10.0 if mw else 0) for i, name in enumerate(GEN_ORDER)}
            gen_statuses = []
            for name in GEN_ORDER:
                is_on = bool(bits and bits[GEN_MAP[name]['coil_read']])
                cap = gen_mw[name]
                if is_on and cap > 1.0:
                    app.gen_memory[name] = cap   # memori daya aktual saat operasi normal
                gen_statuses.append({'name': name, 'type': GEN_MAP[name]['type'], 'mw': cap,
                                     'rated': GEN_MAP[name]['rated'],
                                     'status': 'ONLINE' if is_on else 'OFFLINE'})
            total_gen = sum(gen_mw.values())

            # Pengukuran waktu shedding: generator yang baru terbaca OFFLINE
            gen_online_now = {g['name'] for g in gen_statuses if g['status'] == 'ONLINE'}
            if prev_gen_online is not None and prev_gen_online - gen_online_now:
                timing_on_gen_offline(sorted(prev_gen_online - gen_online_now))
            prev_gen_online = gen_online_now

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

            current_tripped = {l['id'] for l in app.last_load_statuses if l['status'] == 'TRIPPED'}

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
            app.feeder_contingency = feeder_offline

            # ── 4a. OVER-FREQUENCY: diserahkan ke governor droop (tanpa menyalakan beban mendadak)
            if freq_hz > 51.5:
                shed_set = current_tripped.copy()
                log_msg = f"OVER-FREQUENCY ({freq_hz:.2f}Hz): Governor droop meredam kelebihan generasi daya secara alami."

            # ── 4b. UFLS (frekuensi <= 49.25 Hz)
            elif freq_hz <= 49.25:
                if capacity_deficit == 0:
                    # Gangguan frekuensi tanpa generator trip (misal beban melonjak)
                    calc_deficit = max(currently_shed_mw + potential_total_demand * 0.1, potential_total_demand * 0.1)
                else:
                    calc_deficit = max(capacity_deficit, currently_shed_mw)
                droop_tol = min(3.0, max(1.0, calc_deficit * 0.05)) if calc_deficit > 10.0 else 0.0
                effective_deficit = max(0.0, calc_deficit - droop_tol)

                if currently_shed_mw >= effective_deficit and app.shed_holdoff > 0:
                    app.shed_holdoff -= 1
                    shed_set = current_tripped.copy()
                    log_msg = (f"DEFISIT TERTANGANI. Menahan kondisi pasca-pemotongan "
                               f"({app.shed_holdoff} siklus lagi, {shed_mw_of(shed_set, live_loads):.0f}MW).")
                elif currently_shed_mw >= effective_deficit and freq_hz > 48.0:
                    # Sudah cukup: kunci kondisi trip (anti-osilasi)
                    shed_set = current_tripped.copy()
                    log_msg = f"DEFISIT TERTANGANI. Mempertahankan {len(shed_set)} beban mati ({shed_mw_of(shed_set, live_loads):.0f}MW)."
                else:
                    new_shed_set, milp_status = solve_milp_shedding(calc_deficit, live_loads, current_tripped, freq_hz=freq_hz)
                    log_extra = ""
                    if milp_status != 'Optimal':
                        new_shed_set = {l['name'] for l in live_loads}
                        apply_breaker_shed_set(new_shed_set, live_loads)
                    else:
                        # Fast trip instan, lalu validasi AC pasca-pemutusan
                        if new_shed_set != current_tripped:
                            apply_breaker_shed_set(new_shed_set, live_loads)
                            print(f"[FAST TRIP UFLS] Breaker dieksekusi instan via MILP: {sorted(new_shed_set)}")
                        new_shed_set, log_extra = ac_validate_and_swap(
                            new_shed_set, calc_deficit, live_loads, current_tripped, gen_statuses, freq_hz,
                            " [Post-AC Swap: Re-run MILP Bus Sensitivity]", "UFLS")
                    shed_set = new_shed_set
                    log_msg = (f"UFLS TRIGGERED! Melepas {len(shed_set)} beban "
                               f"({shed_mw_of(shed_set, live_loads):.0f}MW): {', '.join(sorted(shed_set))}{log_extra}")
                    if len(shed_set) > len(current_tripped):
                        app.shed_holdoff = SHED_HOLDOFF_CYCLES

            # ── 4c. DEFISIT KAPASITAS (generator trip / demand > kapasitas)
            elif capacity_deficit > 0:
                calc_deficit = capacity_deficit
                droop_tol = min(3.0, max(1.0, calc_deficit * 0.05)) if calc_deficit > 10.0 else 0.0
                effective_deficit = max(0.0, calc_deficit - droop_tol)

                current_online_gens = {g['name'] for g in online_gens}
                gen_trip_occurred = current_online_gens != app.last_online_gens
                app.last_online_gens = current_online_gens

                if app.shed_holdoff > 0:
                    app.shed_holdoff -= 1
                    shed_set = current_tripped.copy()
                    log_msg = (f"DEFISIT TERTANGANI. Menahan kondisi pasca-pemotongan "
                               f"({app.shed_holdoff} siklus lagi, {shed_mw_of(shed_set, live_loads):.0f}MW).")

                # Dead-band histeresis 3.5 MW: jangan potong beban tambahan karena riak noise
                elif not gen_trip_occurred and currently_shed_mw >= (effective_deficit - 3.5) and freq_hz > 49.0:
                    shed_set = current_tripped.copy()
                    log_msg = f"DEFISIT TERTANGANI. Mempertahankan {len(shed_set)} beban mati ({shed_mw_of(shed_set, live_loads):.0f}MW)."

                elif currently_shed_mw < (effective_deficit - 1.0) or gen_trip_occurred:
                    new_shed_set, milp_status = solve_milp_shedding(calc_deficit, live_loads, current_tripped, freq_hz=freq_hz)
                    if milp_status == 'Optimal':
                        if new_shed_set != current_tripped:
                            apply_breaker_shed_set(new_shed_set, live_loads)
                            print(f"[FAST TRIP GEN] Breaker coil dieksekusi instan via MILP: {sorted(new_shed_set)}")
                        new_shed_set, log_extra = ac_validate_and_swap(
                            new_shed_set, calc_deficit, live_loads, current_tripped, gen_statuses, freq_hz,
                            " [Post-AC Swap: Re-run MILP Bus Sensitivity]", "GEN")
                        shed_set = new_shed_set
                        log_msg = (f"DEFISIT TERDETEKSI! Melepas {len(shed_set)} beban "
                                   f"({shed_mw_of(shed_set, live_loads):.0f}MW): {', '.join(sorted(shed_set))}{log_extra}")
                        if len(new_shed_set) > len(current_tripped):
                            app.shed_holdoff = SHED_HOLDOFF_CYCLES
                    else:
                        shed_set = current_tripped.copy()

                elif app.priority_changed:
                    # Operator mengubah prioritas di HMI -> evaluasi ulang konfigurasi trip
                    app.priority_changed = False
                    new_shed_set, milp_status = solve_milp_shedding(calc_deficit, live_loads, current_tripped, freq_hz=freq_hz)
                    if new_shed_set != current_tripped and milp_status == 'Optimal':
                        apply_breaker_shed_set(new_shed_set, live_loads)
                        new_shed_set, log_extra = ac_validate_and_swap(
                            new_shed_set, calc_deficit, live_loads, current_tripped, gen_statuses, freq_hz,
                            " [Post-AC Swap: Re-run MILP]")
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

                elif app.feeder_contingency:
                    # Feeder masih offline: shed awal sekali, lalu tahan dan tunggu feeder pulih
                    is_valid_fc, viol_buses = verify_ac_post_shedding(gen_statuses, live_loads, current_tripped, app.line_statuses)
                    if not is_valid_fc:
                        if len(current_tripped) == 0:
                            shed_set_fc, reason = solve_feeder_contingency_shedding(
                                gen_statuses, live_loads, current_tripped, app.line_statuses, viol_buses)
                            if shed_set_fc:
                                shed_set = shed_set_fc
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
                    is_valid, viol_buses = verify_ac_post_shedding(gen_statuses, live_loads, current_tripped, app.line_statuses)
                    if not is_valid:
                        shed_set_targeted, reason = solve_feeder_contingency_shedding(
                            gen_statuses, live_loads, current_tripped, app.line_statuses, viol_buses)
                        if shed_set_targeted:
                            shed_set = shed_set_targeted
                            log_msg = (f"TOPOLOGY TRIGGER [{reason}]! Melepas {len(shed_set)} beban "
                                       f"({shed_mw_of(shed_set, live_loads):.0f}MW) untuk menyelamatkan tegangan / overload.")
                            if len(shed_set) > len(current_tripped):
                                app.shed_holdoff = SHED_HOLDOFF_CYCLES
                        else:
                            log_msg = "TOPOLOGY TRIGGER: Gagal menemukan solusi shedding, mempertahankan state."

                    elif len(shed_set) == 0:
                        app.restore_timer = 0
                        app.restore_cooldown = 0
                        log_msg = "SISTEM NORMAL: Seluruh beban beroperasi penuh (100% tersuplai)."

                    elif app.restore_cooldown > 0:
                        # Jeda setelah menyalakan satu beban
                        app.restore_cooldown -= 1
                        log_msg = (f"RESTORASI BERTAHAP: Menstabilkan frekuensi & governor "
                                   f"({app.restore_cooldown * 0.1:.1f}s lagi, f={freq_hz:.2f}Hz, sisa {len(shed_set)} beban mati).")

                    elif not (49.95 <= freq_hz <= 50.15):
                        app.restore_timer = 0
                        log_msg = f"RESTORASI DITAHAN: Menunggu frekuensi stabil di 49.95–50.15 Hz (saat ini {freq_hz:.2f}Hz)."

                    else:
                        app.restore_timer += 1
                        if app.restore_timer < RESTORE_STABLE_CYCLES:
                            log_msg = (f"RESTORASI: Memverifikasi stabilitas sistem... "
                                       f"({app.restore_timer * 0.1:.1f}s / {RESTORE_STABLE_CYCLES * 0.1:.1f}s, "
                                       f"f={freq_hz:.2f}Hz, {len(shed_set)} beban menunggu).")
                        else:
                            # Nyalakan tepat 1 beban: prioritas tertinggi dulu (4 -> 3 -> 2),
                            # pada prioritas sama yang terkecil dulu (ramah governor)
                            shed_candidates = sorted(
                                (l for l in live_loads if l['name'] in shed_set),
                                key=lambda x: (-x.get('priority', 2), x.get('nominal_mw', x.get('mw', 0))))
                            current_online_demand = sum(l.get('nominal_mw', l['mw']) for l in live_loads if l['name'] not in shed_set)
                            true_reserve = available_capacity - current_online_demand

                            restored_load = None
                            for cand in shed_candidates:
                                cand_mw = load_memory.get(cand['name'], cand.get('nominal_mw', cand['max_mw']))
                                if (cand_mw + 3.0) <= true_reserve:   # cadangan + margin 3 MW
                                    proposed = shed_set - {cand['name']}
                                    if verify_ac_post_shedding(gen_statuses, live_loads, proposed, app.line_statuses)[0]:
                                        restored_load = cand
                                        shed_set = proposed
                                        break
                                    log_msg = f"RESTORASI TERTUNDA: {cand['name']} memicu kendala tegangan/saluran pada AC Load Flow."
                                else:
                                    log_msg = (f"RESTORASI TERTUNDA: Cadangan daya ({true_reserve:.1f}MW) "
                                               f"belum cukup untuk {cand['name']} ({cand_mw:.1f}MW).")

                            app.restore_timer = 0
                            if restored_load:
                                apply_breaker_shed_set(shed_set, live_loads)
                                r_mw = load_memory.get(restored_load['name'], restored_load.get('nominal_mw', restored_load['mw']))
                                tag = f"{restored_load['name']} (P{restored_load['priority']}"
                                print(f"[RESTORASI BERTAHAP] Breaker {tag}, {r_mw:.1f}MW) DINYALAKAN.")
                                app.restore_cooldown = RESTORE_COOLDOWN_CYCLES
                                if shed_set:
                                    log_msg = (f"RESTORASI: Menyalakan {tag}, {r_mw:.0f}MW). "
                                               f"Jeda 4.0s sebelum beban berikutnya ({len(shed_set)} tersisa).")
                                else:
                                    log_msg = f"RESTORASI LENGKAP: Menyalakan {tag}, {r_mw:.0f}MW). Seluruh beban berhasil pulih 100%!"

            # ── 5. Matriks preselection N-1 (tampilan HMI; dihitung ulang bila status berubah / tiap 1 s)
            app.contingency_counter += 1
            key = (frozenset(g['name'] for g in online_gens), frozenset(shed_set))
            if key != app.contingency_key or app.contingency_counter >= CONTINGENCY_REFRESH:
                matrix = {}
                for g in gen_statuses:
                    pred_shed = []
                    if g['status'] == 'ONLINE':
                        sim_deficit = potential_total_demand - (available_capacity - g['rated'])
                        if sim_deficit > 0:
                            pred_shed = list(solve_milp_shedding(sim_deficit, live_loads, set())[0])
                    matrix[g['name']] = pred_shed
                app.contingency_matrix, app.contingency_key, app.contingency_counter = matrix, key, 0

            # ── 6. Tulis status breaker ke PLC (hanya jika berubah) + feedback PLC
            load_statuses = [{'id': l['name'], 'status': 'TRIPPED' if l['name'] in shed_set else 'NORMAL',
                              'mw': l['actual_mw'], 'priority': l['priority'],
                              'default_priority': l.get('default_priority', 2)} for l in live_loads]
            apply_breaker_shed_set(shed_set, live_loads)

            fb_msg, fb_info = evaluate_plc_feedback(load_statuses, fb_bits)
            while timing_log_queue:
                t_msg = timing_log_queue.pop(0)
                fb_msg = f"{fb_msg} | {t_msg}" if fb_msg else t_msg
            if fb_msg:
                log_msg = fb_msg if log_msg.startswith("Sistem Stabil") else f"{log_msg} | {fb_msg}"
            app.last_load_statuses = load_statuses

            # ── 7. Live load flow (saat status breaker berubah atau tiap 30 siklus)
            app.lf_timer += 1
            breaker_state = "-".join([g['status'] for g in gen_statuses] + [l['status'] for l in load_statuses])
            if app.last_breaker_state != breaker_state or app.lf_timer >= 30:
                app.last_breaker_state = breaker_state
                app.lf_timer = 0
                threading.Thread(target=_run_live_loadflow_bg,
                                 args=(copy.deepcopy(gen_statuses), copy.deepcopy(live_loads),
                                       set(shed_set), dict(app.line_statuses)),
                                 daemon=True).start()

            # ── 8. Log & broadcast ke HMI
            current_active_load = sum(l['actual_mw'] for l in live_loads)
            print(f"| GEN:{total_gen:.0f}MW f={freq_hz:.2f}Hz "
                  f"LOAD:{current_active_load:.0f}MW DEF:{max(0, capacity_deficit):.0f}MW | {plc_time} |")

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
                'log':         f"[{time.strftime('%H:%M:%S')}] {log_msg}",
            })

        except Exception as e:
            print(f"[ERROR Monitor] {e}")

        time.sleep(0.1)


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
@socketio.on('gen_control')
def handle_gen_control(data):
    """ON/OFF breaker generator (bit perintah %M50-%M53)."""
    name, action = data.get('name'), data.get('action')
    if name in GEN_MAP:
        if action == 'OFF':
            gen_cmd_time[name] = time.perf_counter()   # awal pengukuran waktu shedding
        with plc_lock:
            client.write_coil(GEN_MAP[name]['coil_write'], value=(action == 'OFF'))
        print(f">>> Command Web: {action} {name}")


@socketio.on('toggle_line')
def handle_toggle_line(data):
    """ON/OFF saluran transmisi (feeder contingency)."""
    name, action = data.get('name'), data.get('action')
    if name in app.line_statuses:
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
        vals = [max(0, min(32767, int(v))) for v in data.get('gens', [0, 0, 0, 0])]
        if len(vals) != 4:
            print("[INJECT GEN] Data tidak valid — harus 4 nilai.")
            return
        with plc_lock:
            client.write_registers(address=ADDR_SENSOR_GEN, values=vals)
        info = ', '.join(f"{name}={v}MW" for name, v in zip(GEN_ORDER, vals))
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

    threading.Thread(target=background_monitoring, daemon=True).start()

    print("Memulai Web Server di Port 5000...")
    socketio.run(app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)
