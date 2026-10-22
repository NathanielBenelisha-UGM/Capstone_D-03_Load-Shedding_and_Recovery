from flask import Flask, render_template, request, send_from_directory, jsonify
from flask_socketio import SocketIO
from plc_modbus import M241Client as ModbusTcpClient
import threading
import time
import pulp
import os
import logging
from loadflow_module import solve_milp_shedding, run_live_loadflow, verify_ac_post_shedding, solve_socp_fallback, create_network, solve_feeder_contingency_shedding

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.priority_changed = False
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
# %MW0  – %MW11  : Nilai beban aktual (ditulis load.py tiap siklus)
# %MW20 – %MW21  : Jam & Menit simulasi
# %MW30 – %MW33  : Sensor generasi PLTA/PLTS/PLTGU/PLTB (MW)
# %MW40 – %MW51  : Override per-channel dari HMI
#   Nilai  0  = AUTO  → load.py hitung sendiri (sensor lapangan normal)
#   Nilai >0  = CONFIG → load.py pakai nilai ini (sensor mati / force value)
#   Contoh: %MW40=0 → L101 auto | %MW40=12 → L101 dipaksa 12 MW

ADDR_SENSOR_LOAD = 0    # %MW0–%MW11
ADDR_CLOCK       = 20   # %MW20–%MW21
ADDR_SENSOR_GEN  = 30   # %MW30–%MW33
ADDR_OVERRIDE    = 40   # %MW40–%MW51  (per-channel override/config)

# =========================================================
# DATA GENERATOR & BEBAN
# =========================================================
# GENERATOR MAP — cap dibaca DINAMIS dari %MW30–%MW33 setiap siklus
# coil_read/write = nomor coil PLC untuk ON/OFF breaker
GEN_MAP = {
    'PLTA':  {'coil_read': 0, 'coil_write': 50, 'mw_addr': 30, 'type': 'hydro',      'rated': 75},
    'PLTS':  {'coil_read': 1, 'coil_write': 51, 'mw_addr': 31, 'type': 'solar_bess', 'rated': 25},
    'PLTGU': {'coil_read': 2, 'coil_write': 52, 'mw_addr': 32, 'type': 'gas',        'rated': 75},
    'PLTB':  {'coil_read': 3, 'coil_write': 53, 'mw_addr': 33, 'type': 'wind',       'rated': 25},
}
GEN_ORDER = ['PLTA', 'PLTS', 'PLTGU', 'PLTB']
ADDR_FREQ = 54   # %MW54 — frekuensi × 100 (5000 = 50.00 Hz)

LOADS = [
    # %MW0–%MW5 -> Prioritas 2 (non-esensial)
    {'name': 'L101', 'priority': 2, 'default_priority': 2, 'coil_write': 21, 'mw_addr': 0, 'max_mw': 20},
    {'name': 'L201', 'priority': 2, 'default_priority': 2, 'coil_write': 22, 'mw_addr': 1, 'max_mw': 20},
    {'name': 'L303', 'priority': 2, 'default_priority': 2, 'coil_write': 23, 'mw_addr': 2, 'max_mw': 15},
    {'name': 'L304', 'priority': 2, 'default_priority': 2, 'coil_write': 24, 'mw_addr': 3, 'max_mw': 20},
    {'name': 'L305', 'priority': 2, 'default_priority': 2, 'coil_write': 25, 'mw_addr': 4, 'max_mw': 30},
    {'name': 'L401', 'priority': 2, 'default_priority': 2, 'coil_write': 26, 'mw_addr': 5, 'max_mw': 30},
    # %MW6–%MW8 -> Prioritas 3 (esensial)
    {'name': 'L301', 'priority': 3, 'default_priority': 3, 'coil_write': 31, 'mw_addr': 6, 'max_mw': 5},
    {'name': 'L302', 'priority': 3, 'default_priority': 3, 'coil_write': 32, 'mw_addr': 7, 'max_mw': 10},
    {'name': 'L402', 'priority': 3, 'default_priority': 3, 'coil_write': 33, 'mw_addr': 8, 'max_mw': 5},
    # %MW9–%MW11 -> Prioritas 4 (kritis)
    {'name': 'L403', 'priority': 4, 'default_priority': 4, 'coil_write': 41, 'mw_addr': 9, 'max_mw': 10},
    {'name': 'L404', 'priority': 4, 'default_priority': 4, 'coil_write': 42, 'mw_addr': 10, 'max_mw': 15},
    {'name': 'L405', 'priority': 4, 'default_priority': 4, 'coil_write': 43, 'mw_addr': 11, 'max_mw': 20},
]

MILP_PRIORITY_WEIGHTS = {2: 1, 3: 10, 4: 100}

last_live_loads = []
load_memory     = {}
app.line_statuses = {
    'Line_1': 'ONLINE',
    'Line_1-1': 'ONLINE',
    'Line_1-2': 'ONLINE',
    'Line_2': 'ONLINE'
}

def apply_breaker_shed_set(target_shed_set, live_loads):
    """
    Eksekusi cepat pemutusan/penyambungan breaker ke PLC Modbus (<2 ms).
    Digunakan untuk aksi proteksi instan (Fast Trip).
    """
    with plc_lock:
        for load in live_loads:
            tripped = load['name'] in target_shed_set
            client.write_coil(load['coil_write'], value=tripped)



# =========================================================
# PROFIL BEBAN HARIAN (WBP/LWBP)
# =========================================================
def get_load_pct(hour):
    profile = {0: 0.41, 4: 0.32, 6: 0.59, 9: 0.72,
               12: 0.77, 15: 0.72, 17: 0.81, 19: 0.90,
               21: 0.77, 23: 0.54, 24: 0.41}
    hs = sorted(profile.keys())
    for i in range(len(hs) - 1):
        if hs[i] <= hour <= hs[i + 1]:
            h1, h2, p1, p2 = hs[i], hs[i + 1], profile[hs[i]], profile[hs[i + 1]]
            return p1 if h2 == h1 else p1 + ((hour - h1) / (h2 - h1)) * (p2 - p1)
    return 0.45




# =========================================================
# SOCKET — DETEKSI KONEKSI
# =========================================================
@socketio.on('connect')
def handle_connect():
    print(">>> WEB BROWSER TERHUBUNG KE SERVER <<<")


# =========================================================
# BACKGROUND MONITORING
# =========================================================
def background_monitoring():
    print("--- Background Monitoring Dimulai ---")
    while True:
        try:
            with plc_lock:
                if not client.connect():
                    print("Menunggu koneksi ke PLC...")
                    time.sleep(2)
                    continue

            # ── 1. Baca output generator dari %MW30–%MW33 (DINAMIS)
            gen_mw = {}      # {name: mw_actual}
            gen_statuses = []
            with plc_lock:
                gen_reg = client.read_holding_registers(
                    address=ADDR_SENSOR_GEN, count=4)
            if not gen_reg.isError():
                for i, name in enumerate(GEN_ORDER):
                    gen_mw[name] = gen_reg.registers[i] / 10.0
            else:
                for name in GEN_ORDER:
                    gen_mw[name] = 0

            # Inisialisasi gen_memory jika belum ada
            if not hasattr(app, 'gen_memory'):
                app.gen_memory = {}

            # Baca status breaker generator (coil)
            with plc_lock:
                for name in GEN_ORDER:
                    r = client.read_coils(GEN_MAP[name]['coil_read'], count=1)
                    is_on = not r.isError() and r.bits[0]
                    cap   = gen_mw[name]   # cap = actual output dari sensor
                    
                    # Simpan memori daya aktual generator saat beroperasi normal (> 1 MW)
                    if is_on and cap > 1.0:
                        app.gen_memory[name] = cap
                    
                    gen_statuses.append({
                        'name':   name,
                        'type':   GEN_MAP[name]['type'],
                        'mw':     cap,
                        'rated':  GEN_MAP[name]['rated'],
                        'status': 'ONLINE' if is_on else 'OFFLINE',
                    })

            # total_gen = sum output aktual dari sensor (bukan cap statis)
            total_gen = sum(gen_mw.values())

            # ── 2. Baca frekuensi dari %MW54
            freq_hz = 50.0
            with plc_lock:
                freq_reg = client.read_holding_registers(address=ADDR_FREQ, count=1)
            if not freq_reg.isError():
                freq_hz = freq_reg.registers[0] / 100.0
            
            # ── 3. Baca jam simulasi %MW20–%MW21
            sim_hour = 19.0
            with plc_lock:
                clock_result = client.read_holding_registers(
                    address=ADDR_CLOCK, count=2)
            if not clock_result.isError() and len(clock_result.registers) >= 2:
                plc_hour = clock_result.registers[0]
                plc_min  = clock_result.registers[1]
                plc_time = f"{plc_hour:02d}:{plc_min:02d}"
                sim_hour = (plc_hour + plc_min / 60.0) % 24
            else:
                plc_time = time.strftime('%H:%M')

            load_pct = get_load_pct(sim_hour)

            # Baca override (%MW40–%MW51) jika ada
            with plc_lock:
                ov_result = client.read_holding_registers(address=ADDR_OVERRIDE, count=12)
            override = list(ov_result.registers) if not ov_result.isError() else [0]*12

            # Ambil status beban yang sedang tripped dari siklus sebelumnya
            current_tripped = {l['id'] for l in getattr(app, 'last_load_statuses', []) if l['status'] == 'TRIPPED'}

            # ── 4. Baca beban dari %MW0–%MW11
            global last_live_loads, load_memory
            live_loads = []
            with plc_lock:
                reg_result = client.read_holding_registers(
                    address=ADDR_SENSOR_LOAD, count=12)

            if not reg_result.isError():
                sensor_mw = reg_result.registers
                for i, load in enumerate(LOADS):
                    actual_mw = sensor_mw[i] / 10.0
                    
                    # Target nominal un-shed beban pada jam ini
                    if override[i] > 0:
                        nominal_mw = min(override[i] / 10.0, load['max_mw'])
                    else:
                        nominal_mw = load['max_mw'] * load_pct
                    
                    is_tripped = load['name'] in current_tripped
                    if is_tripped:
                        # Beban sedang padam: gunakan daya aktual terakhir sebelum trip dari memory
                        eff_mw = load_memory.get(load['name'], nominal_mw)
                    else:
                        # Beban sedang ONLINE: 100% gunakan daya aktual dari sensor Modbus saat ini!
                        eff_mw = actual_mw
                        if actual_mw > 0.5:
                            load_memory[load['name']] = actual_mw
                    
                    live_loads.append({**load, 'mw': eff_mw,
                                       'actual_mw': actual_mw,
                                       'nominal_mw': nominal_mw})
                last_live_loads = live_loads
            elif last_live_loads:
                live_loads = last_live_loads
            else:
                time.sleep(2)
                continue

            # ── 5. Evaluasi Kapasitas & Load Shedding / Restoration
            potential_total_demand = sum(l['mw'] for l in live_loads)
            available_capacity = sum(g['rated'] for g in gen_statuses if g['status'] == 'ONLINE')
            
            # Hitung daya aktual yang hilang akibat generator yang trip (Loss of Generation)
            # Default ke rated * 0.9 jika baru startup dan belum ada memory
            lost_gen_mw = sum(app.gen_memory.get(g['name'], g['rated'] * 0.9) for g in gen_statuses if g['status'] == 'OFFLINE')
            overload_deficit = max(0.0, potential_total_demand - available_capacity)
            
            # Defisit nyata: daya generator yang lepas saat itu atau kelebihan demand
            capacity_deficit = max(lost_gen_mw, overload_deficit)
            spinning_reserve = available_capacity - total_gen
            
            currently_shed_mw = sum(l['mw'] for l in live_loads if l['name'] in current_tripped)
            
            shed_set = set()
            log_msg = "Sistem Stabil."

            # Deteksi feeder contingency dari status line saat ini
            _feeder_offline = any(s == 'OFFLINE' for s in app.line_statuses.values())
            if not _feeder_offline and getattr(app, 'feeder_contingency', False):
                # Feeder baru kembali online — reset counter, izinkan restorasi penuh
                app.feeder_stable_count = 0
                if not hasattr(app, 'restore_timer'): app.restore_timer = 0
                app.restore_timer = 0
                if not hasattr(app, 'restore_cooldown'): app.restore_cooldown = 0
                app.restore_cooldown = 0
                print("[FEEDER RESTORED] Semua feeder kembali — mode normal, restorasi penuh dimulai.")
            app.feeder_contingency = _feeder_offline

            # Over-Frequency (f > 51.5 Hz):
            # Penanganan kelebihan frekuensi diserahkan ke Governor Droop di sisi pembangkit (load.py),
            # BUKAN dengan menyala-matikan beban konsumen secara mendadak (mencegah chattering & inrush current).
            # Beban yang sedang padam tetap dipertahankan sampai sistem stabil untuk direstorasi bertahap.
            if freq_hz > 51.5:
                shed_set = current_tripped.copy()
                log_msg = f"OVER-FREQUENCY ({freq_hz:.2f}Hz): Governor droop meredam kelebihan generasi daya secara alami."

            # UFLS Trigger (Under-Frequency Load Shedding)
            # Hanya memutus beban jika frekuensi benar-benar anjlok (Simulasi fisis riil)
            elif freq_hz <= 49.25:
                if not hasattr(app, 'shed_holdoff'):
                    app.shed_holdoff = 0

                calc_deficit = max(capacity_deficit, 0)
                if calc_deficit == 0:
                    # Gangguan frekuensi tanpa generator trip (misal beban melonjak mendadak)
                    calc_deficit = max(currently_shed_mw + (potential_total_demand * 0.1), potential_total_demand * 0.1)
                else:
                    # Defisit terukur akibat hilangnya generator secara presisi dari breaker
                    calc_deficit = max(capacity_deficit, currently_shed_mw)

                droop_tol = min(3.0, max(1.0, calc_deficit * 0.05)) if calc_deficit > 10.0 else 0.0
                effective_deficit = max(0.0, calc_deficit - droop_tol)
                    
                if currently_shed_mw >= effective_deficit and app.shed_holdoff > 0:
                    # Sudah melepas beban yang mencukupi defisit, beri jeda inersia bagi generator
                    app.shed_holdoff -= 1
                    shed_set = current_tripped.copy()
                    shed_mw = sum(l['mw'] for l in live_loads if l['name'] in shed_set)
                    log_msg = f"DEFISIT TERTANGANI. Menahan kondisi pasca-pemotongan ({app.shed_holdoff} siklus lagi, {shed_mw:.0f}MW)."
                elif currently_shed_mw >= effective_deficit and freq_hz > 48.0:
                    # Sudah cukup beban yang dilepas, pertahankan kondisi trip agar tidak berganti-ganti (oscillation)
                    shed_set = current_tripped.copy()
                    shed_mw = sum(l['mw'] for l in live_loads if l['name'] in shed_set)
                    log_msg = f"DEFISIT TERTANGANI. Mempertahankan {len(shed_set)} beban mati ({shed_mw:.0f}MW)."
                else:
                    new_shed_set, milp_status = solve_milp_shedding(calc_deficit, live_loads, current_tripped, freq_hz=freq_hz)
                    if milp_status != 'Optimal':
                        new_shed_set = {load['name'] for load in live_loads}
                        apply_breaker_shed_set(new_shed_set, live_loads)
                        log_msg_extra = ""
                    else:
                        # ── 1. FAST BREAKER EXECUTION (INSTANT TRIP ~16-20 ms) ──
                        # Segera putus beban ke PLC tanpa menunggu verifikasi AC
                        # agar frekuensi tidak anjlok ke nadir kritis (<49.5 Hz).
                        if new_shed_set != current_tripped:
                            apply_breaker_shed_set(new_shed_set, live_loads)
                            print(f"[FAST TRIP UFLS] Breaker dieksekusi instan via MILP: {sorted(new_shed_set)}")

                        # ── 2. POST-SHEDDING AC VALIDATION ──
                        # Verifikasi AC hanya dilakukan pasca-pemutusan sebagai validasi tegangan & aliran saluran
                        is_valid, viol_buses = verify_ac_post_shedding(gen_statuses, live_loads, new_shed_set, app.line_statuses)
                        log_msg_extra = ""

                        # ── 3. AC RECTIFICATION / LOAD SWAP (HANYA JIKA AC TIDAK LOLOS) ──
                        # Jika verifikasi AC mendeteksi bus undervoltage atau feeder overload:
                        # Baru verifikasi AC / solver sekunder menukar beban yang dimatikan.
                        if not is_valid:
                            if len(viol_buses) <= 2 and "ALL_BUSES" not in viol_buses:
                                net, l_defs = create_network()
                                viol_loads = []
                                for ld in l_defs:
                                    b_index = ld['bus']
                                    if b_index in viol_buses:
                                        viol_loads.append(ld['name'])
                                
                                adjusted_shed_set, _ = solve_milp_shedding(calc_deficit, live_loads, current_tripped, must_shed_loads=viol_loads, freq_hz=freq_hz)
                                log_msg_extra = " [Post-AC Swap: Re-run MILP Bus Sensitivity]"
                            else:
                                adjusted_shed_set_socp, socp_status = solve_socp_fallback(gen_statuses, live_loads, current_tripped, app.line_statuses)
                                if socp_status in ["optimal", "optimal_inaccurate"]:
                                    adjusted_shed_set = adjusted_shed_set_socp
                                else:
                                    adjusted_shed_set = new_shed_set
                                log_msg_extra = " [Post-AC Swap: Fallback SOCP]"
                            
                            # Jika beban perlu disesuaikan/ditukar pasca validasi AC:
                            if adjusted_shed_set != new_shed_set:
                                apply_breaker_shed_set(adjusted_shed_set, live_loads)
                                print(f"[AC SWAP UFLS] Breaker disesuaikan pasca-verifikasi AC: {sorted(adjusted_shed_set)}")
                                new_shed_set = adjusted_shed_set

                    shed_set = new_shed_set
                    shed_mw = sum(l['mw'] for l in live_loads if l['name'] in shed_set)
                    log_msg = (f"UFLS TRIGGERED! Melepas {len(shed_set)} beban "
                                f"({shed_mw:.0f}MW): {', '.join(sorted(shed_set))}{log_msg_extra}")
                    if len(shed_set) > len(current_tripped):
                        app.shed_holdoff = 3

            elif capacity_deficit > 0:
                # Kondisi Defisit Terdeteksi (Kapasitas generator kurang dari demand)
                calc_deficit = max(capacity_deficit, 0)
                droop_tol = min(3.0, max(1.0, calc_deficit * 0.05)) if calc_deficit > 10.0 else 0.0
                effective_deficit = max(0.0, calc_deficit - droop_tol)

                if not hasattr(app, 'shed_holdoff'):
                    app.shed_holdoff = 0
                if not hasattr(app, 'priority_changed'):
                    app.priority_changed = False
                if not hasattr(app, 'last_online_gens'):
                    app.last_online_gens = set()

                current_online_gens = {g['name'] for g in gen_statuses if g['status'] == 'ONLINE'}
                gen_trip_occurred = (current_online_gens != app.last_online_gens)
                app.last_online_gens = current_online_gens

                if app.shed_holdoff > 0:
                    app.shed_holdoff -= 1
                    shed_set = current_tripped.copy()
                    shed_mw = sum(l['mw'] for l in live_loads if l['name'] in shed_set)
                    log_msg = f"DEFISIT TERTANGANI. Menahan kondisi pasca-pemotongan ({app.shed_holdoff} siklus lagi, {shed_mw:.0f}MW)."

                # DEAD-BAND HISTERESIS (Anti-OverShedding akibat fluktuasi noise):
                # Jika beban yang dipotong saat ini sudah mendekati defisit (toleransi deadband 3.5 MW)
                # DAN tidak ada generator baru yang trip, DAN frekuensi sistem tidak kritis (f > 49.0 Hz),
                # maka KUNCI status pemotongan! JANGAN potong beban tambahan hanya karena riak noise 1-2 MW!
                elif not gen_trip_occurred and currently_shed_mw >= (effective_deficit - 3.5) and freq_hz > 49.0:
                    shed_set = current_tripped.copy()
                    shed_mw = sum(l['mw'] for l in live_loads if l['name'] in shed_set)
                    log_msg = f"DEFISIT TERTANGANI. Mempertahankan {len(shed_set)} beban mati ({shed_mw:.0f}MW)."

                elif currently_shed_mw < (effective_deficit - 1.0) or gen_trip_occurred:
                    # Beban yang dilepas belum mencukupi defisit efektif atau terjadi generator trip baru.
                    # Eksekusi MILP untuk pemotongan beban awal secara presisi (~16 ms).
                    new_shed_set, milp_status = solve_milp_shedding(calc_deficit, live_loads, current_tripped, freq_hz=freq_hz)
                    if milp_status == 'Optimal':
                        # ── 1. FAST BREAKER EXECUTION (INSTANT TRIP ~16-20 ms) ──
                        # Segera putus breaker ke PLC tanpa menunggu verifikasi AC!
                        if new_shed_set != current_tripped:
                            apply_breaker_shed_set(new_shed_set, live_loads)
                            print(f"[FAST TRIP GEN] Breaker coil dieksekusi instan via MILP: {sorted(new_shed_set)}")

                        # ── 2. POST-SHEDDING AC VALIDATION ──
                        # Verifikasi AC dijalankan setelah breaker diputus sebagai validasi tegangan & aliran daya
                        is_valid, viol_buses = verify_ac_post_shedding(gen_statuses, live_loads, new_shed_set, app.line_statuses)
                        log_msg_extra = ""

                        # ── 3. AC RECTIFICATION / LOAD SWAP (HANYA JIKA AC TIDAK LOLOS) ──
                        # Jika verifikasi AC mendeteksi bus undervoltage atau feeder overload:
                        # Baru verifikasi AC / solver sekunder menukar beban yang dimatikan.
                        if not is_valid:
                            if len(viol_buses) <= 2 and "ALL_BUSES" not in viol_buses:
                                net, l_defs = create_network()
                                viol_loads = []
                                for ld in l_defs:
                                    b_index = ld['bus']
                                    if b_index in viol_buses:
                                        viol_loads.append(ld['name'])
                                adjusted_shed_set, _ = solve_milp_shedding(calc_deficit, live_loads, current_tripped, must_shed_loads=viol_loads, freq_hz=freq_hz)
                                log_msg_extra = " [Post-AC Swap: Re-run MILP Bus Sensitivity]"
                            else:
                                adjusted_shed_set_socp, socp_status = solve_socp_fallback(gen_statuses, live_loads, current_tripped, app.line_statuses)
                                if socp_status in ["optimal", "optimal_inaccurate"]:
                                    adjusted_shed_set = adjusted_shed_set_socp
                                else:
                                    adjusted_shed_set = new_shed_set
                                log_msg_extra = " [Post-AC Swap: Fallback SOCP]"

                            # Jika hasil evaluasi AC menghasilkan kombinasi beban yang berbeda, lakukan penukaran di PLC
                            if adjusted_shed_set != new_shed_set:
                                apply_breaker_shed_set(adjusted_shed_set, live_loads)
                                print(f"[AC SWAP GEN] Breaker disesuaikan pasca-verifikasi AC: {sorted(adjusted_shed_set)}")
                                new_shed_set = adjusted_shed_set

                        shed_set = new_shed_set
                        shed_mw = sum(l['mw'] for l in live_loads if l['name'] in shed_set)
                        log_msg = f"DEFISIT TERDETEKSI! Melepas {len(shed_set)} beban ({shed_mw:.0f}MW): {', '.join(sorted(shed_set))}{log_msg_extra}"
                        if len(new_shed_set) > len(current_tripped):
                            app.shed_holdoff = 3
                    else:
                        shed_set = current_tripped.copy()

                elif app.priority_changed:
                    # Operator mengubah prioritas beban di HMI -> Evaluasi apakah konfigurasi trip perlu ditukar
                    app.priority_changed = False
                    new_shed_set, milp_status = solve_milp_shedding(calc_deficit, live_loads, current_tripped, freq_hz=freq_hz)
                    if new_shed_set != current_tripped and milp_status == 'Optimal':
                        apply_breaker_shed_set(new_shed_set, live_loads)
                        is_valid, viol_buses = verify_ac_post_shedding(gen_statuses, live_loads, new_shed_set, app.line_statuses)
                        log_msg_extra = ""
                        if not is_valid:
                            if len(viol_buses) <= 2 and "ALL_BUSES" not in viol_buses:
                                net, l_defs = create_network()
                                viol_loads = []
                                for ld in l_defs:
                                    b_index = ld['bus']
                                    if b_index in viol_buses:
                                        viol_loads.append(ld['name'])
                                adjusted_shed_set, _ = solve_milp_shedding(calc_deficit, live_loads, current_tripped, must_shed_loads=viol_loads, freq_hz=freq_hz)
                                log_msg_extra = " [Post-AC Swap: Re-run MILP]"
                            else:
                                adjusted_shed_set_socp, socp_status = solve_socp_fallback(gen_statuses, live_loads, current_tripped, app.line_statuses)
                                if socp_status in ["optimal", "optimal_inaccurate"]:
                                    adjusted_shed_set = adjusted_shed_set_socp
                                else:
                                    adjusted_shed_set = new_shed_set
                                log_msg_extra = " [Post-AC Swap: Fallback SOCP]"

                            if adjusted_shed_set != new_shed_set:
                                apply_breaker_shed_set(adjusted_shed_set, live_loads)
                                new_shed_set = adjusted_shed_set

                        shed_set = new_shed_set
                        shed_mw = sum(l['mw'] for l in live_loads if l['name'] in shed_set)
                        log_msg = f"RE-PRIORITIZED! Menukar beban mati sesuai prioritas operator ({shed_mw:.0f}MW).{log_msg_extra}"
                        if len(new_shed_set) > len(current_tripped):
                            app.shed_holdoff = 3
                    else:
                        shed_set = current_tripped.copy()

                else:
                    # STATE LOCK: Beban yang dimatikan sudah mencukupi defisit dan tidak ada perubahan prioritas operator.
                    # Kunci status trip tanpa kalkulasi ulang berulang (mencegah chattering/hunting 56MW <-> 74MW).
                    shed_set = current_tripped.copy()
                    # Jangan ganggu log_msg agar tidak spam
                           
            else:
                # Kapasitas mencukupi.
                shed_set = current_tripped.copy()

                # Cek holdoff DULU sebelum apapun — sama seperti penanganan gangguan generator.
                # Selama holdoff aktif, skip proactive check agar SOCP tidak re-trigger tiap siklus
                # dan beban tidak mati-nyala berulang.
                if not hasattr(app, 'shed_holdoff'):
                    app.shed_holdoff = 0

                if app.shed_holdoff > 0:
                    app.shed_holdoff -= 1
                    if not hasattr(app, 'restore_timer'):
                        app.restore_timer = 0
                    app.restore_timer = 0
                    if not hasattr(app, 'restore_cooldown'):
                        app.restore_cooldown = 0
                    app.restore_cooldown = 0
                    log_msg = f"RESTORASI DITAHAN: Cooldown pasca-shedding ({app.shed_holdoff} siklus lagi)."

                else:  # holdoff habis
                    if app.feeder_contingency:
                        # ── FEEDER CONTINGENCY MODE ──────────────────────────────────────────
                        # Feeder masih offline: jangan re-trigger SOCP berulang.
                        # Lakukan shed awal jika belum ada, lalu tahan state dan tunggu stabil.
                        is_valid_fc, viol_buses = verify_ac_post_shedding(gen_statuses, live_loads, current_tripped, app.line_statuses)
                        if not is_valid_fc:
                            if len(current_tripped) == 0:
                                # Belum ada shed — jalankan targeted contingency shedding
                                shed_set_fc, reason = solve_feeder_contingency_shedding(gen_statuses, live_loads, current_tripped, app.line_statuses, viol_buses)
                                if shed_set_fc:
                                    shed_set = shed_set_fc
                                    shed_mw = sum(l['mw'] for l in live_loads if l['name'] in shed_set)
                                    log_msg = f"FEEDER TRIP [{reason}]: Shedding {len(shed_set)} beban ({shed_mw:.0f}MW) untuk amankan tegangan & saluran."
                                    app.shed_holdoff = 3
                                else:
                                    shed_set = current_tripped.copy()
                                    log_msg = "FEEDER TRIP: Gagal menemukan skema shedding aman, tahan state."
                            else:
                                # Beban sudah shed — tahan, tidak perlu SOCP ulang
                                shed_set = current_tripped.copy()
                                if not hasattr(app, 'restore_timer'): app.restore_timer = 0
                                app.restore_timer = 0
                                log_msg = "FEEDER CONTINGENCY: Jaringan terkendala (overload gen/topologi). Tahan state, tunggu feeder pulih."
                        else:
                            # AC valid — tunggu frekuensi benar-benar stabil sebelum restore
                            if not hasattr(app, 'feeder_stable_count'): app.feeder_stable_count = 0
                            if not hasattr(app, 'restore_timer'): app.restore_timer = 0

                            if 49.5 <= freq_hz <= 50.5:
                                app.feeder_stable_count += 1
                            else:
                                app.feeder_stable_count = 0
                                app.restore_timer = 0
                                log_msg = f"FEEDER CONTINGENCY: Menunggu frekuensi stabil ({freq_hz:.2f}Hz, perlu 49.5-50.5Hz)."

                            # Feeder masih offline — TIDAK ada restore sama sekali.
                            # Tunggu feeder kembali, baru restorasi penuh di mode normal.
                            if app.feeder_stable_count >= 5:
                                log_msg = f"FEEDER CONTINGENCY: Frekuensi stabil ({freq_hz:.2f}Hz). Tahan beban — tunggu feeder pulih sebelum restore."
                            elif app.feeder_stable_count > 0:
                                log_msg = f"FEEDER CONTINGENCY: Menunggu stabilitas ({app.feeder_stable_count}/5 siklus stabil)."

                    else:
                        # ── NORMAL MODE: proactive targeted + restore path ──────────────────
                        is_valid, viol_buses = verify_ac_post_shedding(gen_statuses, live_loads, current_tripped, app.line_statuses)
                        if not is_valid:
                            shed_set_targeted, reason = solve_feeder_contingency_shedding(gen_statuses, live_loads, current_tripped, app.line_statuses, viol_buses)
                            if shed_set_targeted:
                                shed_set = shed_set_targeted
                                shed_mw = sum(l['mw'] for l in live_loads if l['name'] in shed_set)
                                log_msg = f"TOPOLOGY TRIGGER [{reason}]! Melepas {len(shed_set)} beban ({shed_mw:.0f}MW) untuk menyelamatkan tegangan / overload."
                                if len(shed_set) > len(current_tripped):
                                    app.shed_holdoff = 3
                            else:
                                shed_set = current_tripped.copy()
                                log_msg = "TOPOLOGY TRIGGER: Gagal menemukan solusi shedding, mempertahankan state."

                        else:
                            # ── TAHAP RESTORASI BEBAN BERTAHAP (1 PER SATU, PELAN-PELAN) ──
                            # Dipanggil hanya jika tidak ada gangguan lagi (kapasitas cukup, feeder normal, AC valid)
                            if not hasattr(app, 'restore_timer'):
                                app.restore_timer = 0
                            if not hasattr(app, 'restore_cooldown'):
                                app.restore_cooldown = 0

                            # 1. Jika seluruh beban sudah menyala normal, tidak ada yang perlu direstorasi
                            if len(shed_set) == 0:
                                app.restore_timer = 0
                                app.restore_cooldown = 0
                                log_msg = "SISTEM NORMAL: Seluruh beban beroperasi penuh (100% tersuplai)."

                            # 2. Jika sedang dalam jeda pendinginan (cooldown) pasca-menyalakan 1 beban sebelumnya
                            elif app.restore_cooldown > 0:
                                app.restore_cooldown -= 1
                                sisa = len(shed_set)
                                log_msg = (f"RESTORASI BERTAHAP: Menstabilkan frekuensi & governor "
                                           f"({app.restore_cooldown * 0.1:.1f}s lagi, f={freq_hz:.2f}Hz, sisa {sisa} beban mati).")

                            # 3. Pengecekan stabilitas frekuensi sebelum menyalakan beban berikutnya
                            else:
                                # Frekuensi harus berada pada rentang sangat stabil (49.95 - 50.15 Hz)
                                if 49.95 <= freq_hz <= 50.15:
                                    app.restore_timer += 1
                                    TARGET_STABLE_CYCLES = 10  # 10 siklus @ 0.1s = 1.0 detik frekuensi stabil
                                    
                                    if app.restore_timer < TARGET_STABLE_CYCLES:
                                        sisa = len(shed_set)
                                        log_msg = (f"RESTORASI: Memverifikasi stabilitas sistem... "
                                                   f"({app.restore_timer * 0.1:.1f}s / {TARGET_STABLE_CYCLES * 0.1:.1f}s, "
                                                   f"f={freq_hz:.2f}Hz, {sisa} beban menunggu).")
                                    else:
                                        # ── EKSEKUSI PENYALAAN TEPAT 1 BEBAN ──
                                        # Kumpulkan semua beban yang masih mati
                                        shed_candidates = [l for l in live_loads if l['name'] in shed_set]

                                        # URUTAN PRIORITAS RESTORASI:
                                        # a) Prioritas tertinggi dinyalakan PERTAMA: Prioritas 4 (Kritis) -> 3 (Esensial) -> 2 (Non-Esensial)
                                        # b) Pada prioritas sama, pilih beban terkecil lebih dulu (hentakan daya bertahap/ramah governor)
                                        shed_candidates.sort(key=lambda x: (-x.get('priority', 2), x.get('nominal_mw', x.get('mw', 0))))

                                        # Hitung cadangan daya putar nyata (True Spinning Reserve)
                                        effective_capacity = sum(g['rated'] for g in gen_statuses if g['status'] == 'ONLINE')
                                        current_online_demand = sum(l.get('nominal_mw', l['mw']) for l in live_loads if l['name'] not in shed_set)
                                        true_reserve = effective_capacity - current_online_demand

                                        restored_load = None
                                        for cand in shed_candidates:
                                            cand_mw = load_memory.get(cand['name'], cand.get('nominal_mw', cand['max_mw']))
                                            # Cadangan putar harus cukup untuk cand_mw ditambah safety margin 3 MW
                                            if (cand_mw + 3.0) <= true_reserve:
                                                proposed_shed_set = shed_set.copy()
                                                proposed_shed_set.remove(cand['name'])
                                                
                                                # Validasi AC power flow (tegangan & pembebanan saluran)
                                                is_valid_ac, _ = verify_ac_post_shedding(gen_statuses, live_loads, proposed_shed_set, app.line_statuses)
                                                if is_valid_ac:
                                                    restored_load = cand
                                                    shed_set = proposed_shed_set
                                                    break
                                                else:
                                                    log_msg = f"RESTORASI TERTUNDA: {cand['name']} memicu kendala tegangan/saluran pada AC Load Flow."
                                            else:
                                                log_msg = f"RESTORASI TERTUNDA: Cadangan daya ({true_reserve:.1f}MW) belum cukup untuk {cand['name']} ({cand_mw:.1f}MW)."

                                        if restored_load:
                                            # Eksekusi langsung breaker ke PLC untuk menyalakan beban ini
                                            apply_breaker_shed_set(shed_set, live_loads)
                                            r_mw = load_memory.get(restored_load['name'], restored_load.get('nominal_mw', restored_load['mw']))
                                            print(f"[RESTORASI BERTAHAP] Breaker {restored_load['name']} (P{restored_load['priority']}, {r_mw:.1f}MW) DINYALAKAN.")

                                            # Reset timer dan set jeda cooldown 1.0 detik (10 siklus @ 0.1s) sebelum beban berikutnya
                                            app.restore_timer = 0
                                            app.restore_cooldown = 10  # 1.0 detik jeda antar beban
                                            
                                            sisa_beban = len(shed_set)
                                            if sisa_beban > 0:
                                                log_msg = f"RESTORASI: Menyalakan {restored_load['name']} (P{restored_load['priority']}, {r_mw:.0f}MW). Jeda 4.0s sebelum beban berikutnya ({sisa_beban} tersisa)."
                                            else:
                                                log_msg = f"RESTORASI LENGKAP: Menyalakan {restored_load['name']} (P{restored_load['priority']}, {r_mw:.0f}MW). Seluruh beban berhasil pulih 100%!"
                                        else:
                                            app.restore_timer = 0
                                else:
                                    # Frekuensi di luar batas aman restorasi (misal < 49.95 Hz atau > 50.15 Hz)
                                    app.restore_timer = 0
                                    log_msg = f"RESTORASI DITAHAN: Menunggu frekuensi stabil di 49.95–50.15 Hz (saat ini {freq_hz:.2f}Hz)."

            # Hitung Preselection Matrix (N-1 Generators)
            contingency_matrix = {}
            for g in gen_statuses:
                pred_shed = []
                if g['status'] == 'ONLINE':
                    sim_cap = available_capacity - g['rated']
                    sim_deficit = potential_total_demand - sim_cap
                    if sim_deficit > 0:
                        pred_set, _ = solve_milp_shedding(sim_deficit, live_loads, set())
                        pred_shed = list(pred_set)
                contingency_matrix[g['name']] = pred_shed

            # current_active_load adalah total nilai riil yang terbaca dari sensor (load.py)
            current_active_load = sum(l['actual_mw'] for l in live_loads)
            load_statuses = []
            
            with plc_lock:
                for load in live_loads:
                    tripped = load['name'] in shed_set
                    client.write_coil(load['coil_write'], value=tripped)
                    load_statuses.append({
                        'id':       load['name'],
                        'status':   'TRIPPED' if tripped else 'NORMAL',
                        'mw':       load['actual_mw'],
                        'priority': load['priority'],
                        'default_priority': load.get('default_priority', 2),
                    })
                    
            app.last_load_statuses = load_statuses

            # ── 5.5 LIVE LOAD FLOW TRIGGER
            current_breaker_state = "-".join([g['status'] for g in gen_statuses] + [l['status'] for l in load_statuses])
            
            if not hasattr(app, 'lf_timer'):
                app.lf_timer = 0
            app.lf_timer += 1
            
            # Jalankan Load Flow jika:
            # 1. Terjadi perubahan status breaker (Trip / Nyala)
            # 2. Atau setiap 30 siklus (sekitar 3 detik) untuk melihat live update fluktuasi beban
            if getattr(app, 'last_breaker_state', '') != current_breaker_state or app.lf_timer >= 30:
                app.last_breaker_state = current_breaker_state
                app.lf_timer = 0
                
                # Trigger live load flow in background
                def run_lf_bg(gs, ls, trips, line_stats):
                    try:
                        from loadflow_module import run_live_loadflow
                        res = run_live_loadflow(gs, ls, trips, line_stats)
                        socketio.emit('live_loadflow_result', {'status': 'success', 'data': res})
                    except Exception as e:
                        socketio.emit('live_loadflow_result', {'status': 'error', 'message': str(e)})
                        print(f"[SCADA] Error Live Load Flow: {e}")
                
                # We pass a copy of the current state to the thread
                import copy
                threading.Thread(target=run_lf_bg, args=(copy.deepcopy(gen_statuses), copy.deepcopy(live_loads), copy.deepcopy(shed_set), copy.deepcopy(app.line_statuses)), daemon=True).start()

            # ── 6. Log & Broadcast
            print(f"| GEN:{total_gen:.0f}MW f={freq_hz:.2f}Hz "
                  f"LOAD:{current_active_load:.0f}MW DEF:{max(0,capacity_deficit):.0f}MW "
                  f"| {plc_time} |")

            socketio.emit('grid_update', {
                'total_gen':   total_gen,
                'total_load':  current_active_load,
                'deficit':     max(0, capacity_deficit),
                'frequency':   round(freq_hz, 3),
                'generators':  gen_statuses,
                'sensor_gens': [{'name': n, 'mw': gen_mw[n]} for n in GEN_ORDER],
                'loads':       load_statuses,
                'lines':       app.line_statuses,
                'plc_time':    plc_time,
                'contingency': contingency_matrix,
                'log':         f"[{time.strftime('%H:%M:%S')}] {log_msg}",
            })

        except Exception as e:
            print(f"[ERROR Monitor] {e}")

        time.sleep(0.1)


# =========================================================
# ROUTES
# =========================================================
@app.route('/')
def index():
    role = request.args.get('role', 'viewer')
    return render_template('index.html', role=role)


# =========================================================
# SOCKET — KONTROL GENERATOR (ON/OFF)
# =========================================================
@socketio.on('gen_control')
def handle_gen_control(data):
    """ON/OFF breaker generator via coil."""
    name   = data.get('name')
    action = data.get('action')
    if name in GEN_MAP:
        coil = GEN_MAP[name]['coil_write']
        with plc_lock:
            client.write_coil(coil, value=(action == 'OFF'))
        print(f">>> Command Web: {action} {name}")

@socketio.on('toggle_line')
def handle_toggle_line(data):
    """ON/OFF Saluran Transmisi (Feeder Contingency)."""
    name = data.get('name')
    action = data.get('action') # 'ON' atau 'OFF'
    
    if name in app.line_statuses:
        app.line_statuses[name] = 'ONLINE' if action == 'ON' else 'OFFLINE'
        
        # Trigger live load flow to update immediately
        app.last_breaker_state = "LINE_TOGGLE_TRIGGER"
        
        log_msg = f"SALURAN TRANSMISI DIPUTUS" if action == 'OFF' else f"SALURAN TRANSMISI DISAMBUNG"
        print(f">>> {log_msg}: {name}")


# =========================================================
# SOCKET — OVERRIDE PER-CHANNEL BEBAN KE PLC
# =========================================================
@socketio.on('set_load_interrupt')
def handle_set_load_interrupt(data):
    """
    Override per-channel nilai beban dari HMI.

    data = {
        'loads': [val_L101, val_L201, val_L303, val_L304,
                  val_L305, val_L401, val_L301, val_L302,
                  val_L402, val_L403, val_L404, val_L405]
    }
    Nilai  0  pada index i = AUTO  → load.py kembali hitung sendiri channel i
    Nilai >0  pada index i = CONFIG → channel i dipaksa pakai nilai ini

    Mekanisme dual-write:
    - Tulis ke %MW40–%MW51 → dibaca load.py setiap siklus (persisten)
    - Tulis langsung ke %MW0–%MW11 → efek instan di app.py sebelum siklus berikutnya
    """
    try:
        vals = data.get('loads', [0] * 12)
        vals = [max(0, min(32767, int(round(float(v) * 10)))) for v in vals]
        if len(vals) != 12:
            print("[OVERRIDE BEBAN] Data tidak valid — harus 12 nilai.")
            return

        with plc_lock:
            # 1. Tulis ke %MW40–%MW51 (dibaca load.py tiap siklus)
            client.write_registers(address=ADDR_OVERRIDE, values=vals)
            # 2. Tulis langsung ke %MW0–%MW11 untuk efek instan
            #    (hanya channel yang >0, sisanya biarkan load.py yang tulis)
            for i, v in enumerate(vals):
                if v > 0:
                    client.write_register(address=ADDR_SENSOR_LOAD + i, value=v)

        # Log ke terminal
        override_info = {f'%MW{40+i}={v/10.0}' for i, v in enumerate(vals) if v > 0}
        auto_info     = {f'%MW{40+i}=AUTO' for i, v in enumerate(vals) if v == 0}
        print(f"[OVERRIDE BEBAN] Config: {override_info}")
        if auto_info:
            print(f"[OVERRIDE BEBAN] Auto: {len(auto_info)} channel")

        socketio.emit('interrupt_ack', {
            'type':    'load',
            'message': f"Override beban dikirim. {sum(1 for v in vals if v>0)} channel CONFIG, "
                       f"{sum(1 for v in vals if v==0)} channel AUTO.",
            'values':  vals,
        })
    except Exception as e:
        print(f"[OVERRIDE BEBAN] Error: {e}")


# =========================================================
# SOCKET — INJECT NILAI GENERASI (MW) KE PLC
# =========================================================
@socketio.on('set_gen_interrupt')
def handle_set_gen_interrupt(data):
    """
    Inject nilai generasi (MW) dari HMI ke PLC %MW30–%MW33.
    Nilai 0 = tidak ada generasi (unit mati)
    Nilai >0 = daya output unit generasi tersebut

    data = {'gens': [mw_PLTA, mw_PLTS, mw_PLTGU, mw_PLTB]}
    """
    try:
        vals = data.get('gens', [0, 0, 0, 0])
        vals = [max(0, min(32767, int(v))) for v in vals]
        if len(vals) != 4:
            print("[INJECT GEN] Data tidak valid — harus 4 nilai.")
            return
        with plc_lock:
            client.write_registers(address=ADDR_SENSOR_GEN, values=vals)
        gen_names = ['PLTA', 'PLTS', 'PLTGU', 'PLTB']
        info = ', '.join(f"{gen_names[i]}={vals[i]}MW" for i in range(4))
        print(f"[INJECT GEN] %MW30–%MW33: {info}")
        socketio.emit('interrupt_ack', {
            'type':    'gen',
            'message': f"Generasi dikirim ke PLC: {info}",
            'values':  vals,
        })
    except Exception as e:
        print(f"[INJECT GEN] Error: {e}")

# =========================================================
# SOCKET — UPDATE PRIORITY BEBAN (MILP WEIGHT)
# =========================================================
def persist_priority_to_file(load_name, new_prio):
    import re
    try:
        with open(__file__, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Cari dan replace angka di: {'name': 'L101', 'priority': X, 
        pattern = r"(\{'name':\s*'" + load_name + r"',\s*'priority':\s*)\d+"
        new_content = re.sub(pattern, r"\g<1>" + str(new_prio), content)
        
        with open(__file__, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print(f"[PERSIST] {load_name} priority updated to {new_prio} in app.py")
    except Exception as e:
        print(f"[PERSIST] Failed to rewrite app.py: {e}")

@socketio.on('set_load_priority')
def handle_set_load_priority(data):
    """
    Update prioritas MILP untuk beban tertentu secara in-memory.
    Priority: 2 = Low (NON-ESENSIAL), 3 = Medium (ESENSIAL), 4 = High (KRITIS)
    Semakin tinggi priority, MILP semakin menghindari mematikan beban tersebut.

    data = {'load': 'L101', 'priority': 3}
    """
    load_id  = data.get('load')
    priority = int(data.get('priority', 2))
    priority = max(2, min(4, priority))   # clamp ke range valid 2-4

    updated = False
    for load in LOADS:
        if load['name'] == load_id:
            old = load['priority']
            if old != priority:
                load['priority'] = priority
                updated = True
                app.priority_changed = True
                print(f"[PRIORITY] {load_id}: {old} → {priority}")
                persist_priority_to_file(load_id, priority)
            break

    if not updated:
        print(f"[PRIORITY] Load '{load_id}' tidak ditemukan.")
        return

    label = {2: 'LOW (Non-Esensial)', 3: 'MEDIUM (Esensial)', 4: 'HIGH (Kritis)'}[priority]
    socketio.emit('interrupt_ack', {
        'type':    'priority',
        'message': f"Priority {load_id} diperbarui → {label}",
        'load':    load_id,
        'priority': priority,
    })


# =========================================================
# MENJALANKAN PROGRAM UTAMA
# =========================================================
if __name__ == '__main__':
    print("Mempersiapkan Sistem SCADA...")
    print(f"PLC Target: {PLC_IP}:{PORT}")
    print("Alamat MW:")
    print("  %MW0--%MW11  -> Nilai beban aktual (load.py tulis tiap siklus)")
    print("  %MW20--%MW21 -> Jam simulasi")
    print("  %MW30--%MW33 -> Sensor Generasi PLTA/PLTS/PLTGU/PLTB")
    print("  %MW40--%MW51 -> Override per-channel HMI (0=AUTO, >0=CONFIG/FORCE)")


    monitor_thread = threading.Thread(target=background_monitoring, daemon=True)
    monitor_thread.start()

    print("Memulai Web Server di Port 5000...")
    socketio.run(app, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)