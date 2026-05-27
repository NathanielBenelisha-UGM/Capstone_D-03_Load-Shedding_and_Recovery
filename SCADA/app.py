from flask import Flask, render_template, request, send_from_directory
from flask_socketio import SocketIO
from pymodbus.client import ModbusTcpClient
import threading
import time
import pulp

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

import os

# =========================================================
# KONFIGURASI PLC
# =========================================================
PLC_IP = os.getenv('PLC_IP', '192.168.100.195')
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


# =========================================================
# MILP LOAD SHEDDING SOLVER
# =========================================================
def solve_milp_shedding(deficit, live_loads, current_tripped=set()):
    import pulp
    
    prob = pulp.LpProblem("LoadShedding", pulp.LpMinimize)
    
    shed_vars = {}
    for l in live_loads:
        shed_vars[l['name']] = pulp.LpVariable(f"shed_{l['name']}", cat='Binary')
        
    # Constraint: Total daya yang dilepas harus >= deficit
    prob += pulp.lpSum([l['mw'] * shed_vars[l['name']] for l in live_loads]) >= deficit
    
    # Objective: Minimize cost of shedding
    objective = []
    for l in live_loads:
        prio_level = l.get('priority', 2)
        weight = MILP_PRIORITY_WEIGHTS.get(prio_level, 1)
        
        # Anti-Oscillation: Jika beban sudah mati (tripped), berikan sedikit diskon biaya (10%)
        # Ini mencegah MILP menukar-nukar beban mati yang memiliki prioritas SAMA.
        # Namun diskon ini tidak boleh terlalu besar agar tidak mengalahkan beban dengan prioritas BERBEDA.
        if l['name'] in current_tripped:
            weight = weight * 0.90 
            
        objective.append(weight * shed_vars[l['name']])
        
    prob += pulp.lpSum(objective)

    solver = pulp.PULP_CBC_CMD(msg=0)
    prob.solve(solver)

    status   = pulp.LpStatus[prob.status]
    shed_set = set()
    if status == 'Optimal':
        for load in live_loads:
            if pulp.value(shed_vars[load['name']]) > 0.5:
                shed_set.add(load['name'])

    return shed_set, status


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
                    gen_mw[name] = int(gen_reg.registers[i])
            else:
                for name in GEN_ORDER:
                    gen_mw[name] = 0

            # Baca status breaker generator (coil)
            with plc_lock:
                for name in GEN_ORDER:
                    r = client.read_coils(GEN_MAP[name]['coil_read'], count=1)
                    is_on = not r.isError() and r.bits[0]
                    cap   = gen_mw[name]   # cap = actual output dari sensor
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

            # ── 3. Baca beban dari %MW0–%MW11
            global last_live_loads, load_memory
            live_loads = []
            with plc_lock:
                reg_result = client.read_holding_registers(
                    address=ADDR_SENSOR_LOAD, count=12)

            if not reg_result.isError():
                sensor_mw = reg_result.registers
                for i, load in enumerate(LOADS):
                    actual_mw = int(sensor_mw[i])
                    if actual_mw > 0:
                        load_memory[load['name']] = actual_mw
                    potential_mw = load_memory.get(load['name'], actual_mw)
                    live_loads.append({**load, 'mw': potential_mw,
                                       'actual_mw': actual_mw})
                last_live_loads = live_loads
            elif last_live_loads:
                live_loads = last_live_loads
            else:
                time.sleep(2)
                continue

            # ── 4. Baca jam simulasi %MW20–%MW21
            with plc_lock:
                clock_result = client.read_holding_registers(
                    address=ADDR_CLOCK, count=2)
            if not clock_result.isError() and len(clock_result.registers) >= 2:
                plc_time = (f"{clock_result.registers[0]:02d}:"
                            f"{clock_result.registers[1]:02d}")
            else:
                plc_time = time.strftime('%H:%M')

            # ── 5. Evaluasi Kapasitas & Load Shedding / Restoration
            potential_total_demand = sum(l['mw'] for l in live_loads)
            available_capacity = sum(g['rated'] for g in gen_statuses if g['status'] == 'ONLINE')
            
            capacity_deficit = potential_total_demand - available_capacity
            spinning_reserve = available_capacity - total_gen
            
            # Get currently tripped loads from last cycle
            current_tripped = {l['id'] for l in getattr(app, 'last_load_statuses', []) if l['status'] == 'TRIPPED'}
            currently_shed_mw = sum(l['mw'] for l in live_loads if l['name'] in current_tripped)
            
            shed_set = set()
            log_msg = "Sistem Stabil."
            
            # UFLS Trigger (Under-Frequency Load Shedding)
            # Hanya memutus beban jika frekuensi benar-benar anjlok (Simulasi fisis riil)
            if freq_hz <= 49.0:
                calc_deficit = max(capacity_deficit, 0)
                calc_deficit = max(calc_deficit, currently_shed_mw + (potential_total_demand * 0.1)) # Force shed more
                    
                if currently_shed_mw >= calc_deficit and freq_hz > 48.5:
                    # Sudah cukup beban yang dilepas, pertahankan kondisi trip agar tidak berganti-ganti (oscillation)
                    shed_set = current_tripped.copy()
                    shed_mw = sum(l['mw'] for l in live_loads if l['name'] in shed_set)
                    log_msg = f"DEFISIT TERTANGANI. Mempertahankan {len(shed_set)} beban mati ({shed_mw:.0f}MW)."
                else:
                    # Perlu run MILP karena defisit bertambah atau frekuensi kritis
                    shed_set, milp_status = solve_milp_shedding(calc_deficit, live_loads, current_tripped)
                    if milp_status != 'Optimal':
                        shed_set = {load['name'] for load in live_loads}
                        
                    shed_mw = sum(l['mw'] for l in live_loads if l['name'] in shed_set)
                    log_msg = (f"UFLS TRIGGERED! Melepas {len(shed_set)} beban "
                               f"({shed_mw:.0f}MW): {', '.join(sorted(shed_set))}")

            elif capacity_deficit > 0:
                # Kondisi Defisit Stabil (Frekuensi normal, tapi kapasitas masih kurang)
                # Evaluasi secara diam-diam: Apakah user mengubah prioritas sehingga konfigurasi trip perlu ditukar?
                calc_deficit = max(capacity_deficit, 0)
                new_shed_set, milp_status = solve_milp_shedding(calc_deficit, live_loads, current_tripped)
                
                if new_shed_set != current_tripped and milp_status == 'Optimal':
                    # Ternyata MILP menemukan solusi yang lebih baik (user mengganti prioritas load yang trip menjadi penting)
                    shed_set = new_shed_set
                    shed_mw = sum(l['mw'] for l in live_loads if l['name'] in shed_set)
                    log_msg = f"RE-PRIORITIZED! Menukar beban mati untuk mengamankan VIP ({shed_mw:.0f}MW)."
                else:
                    # Tidak ada perubahan prioritas, pertahankan yang mati
                    shed_set = current_tripped.copy()
                    # Jangan ganggu log_msg agar tidak spam
                           
            else:
                # Kapasitas mencukupi. Coba restore beban yang trip secara bertahap (1 per 1)
                shed_set = current_tripped.copy()
                if len(shed_set) > 0:
                    if not hasattr(app, 'restore_timer'):
                        app.restore_timer = 0
                        
                    # Pastikan tidak ada beban yang sedang soft-start (menunggu generator mengejar)
                    is_settled = True
                    for l in live_loads:
                        if l['name'] not in shed_set:
                            # Jika beban sudah on tapi aktualnya masih < 90% dari potensialnya, berarti masih soft-start
                            if l['mw'] > 5 and l['actual_mw'] < l['mw'] * 0.9:
                                is_settled = False
                                break
                                
                    if 49.95 <= freq_hz <= 50.05 and is_settled:
                        app.restore_timer += 1
                        if app.restore_timer >= 10: # Tunggu 10 siklus (1 detik) agar benar-benar stabil
                            # Hitung reserve berdasarkan beban yang PASTI akan ditarik setelah soft-start selesai
                            expected_on_demand = sum(l['mw'] for l in live_loads if l['name'] not in shed_set)
                            
                            # SCADA Cerdas: Gunakan kapasitas efektif (Aktual + Margin 20 MW per Gen)
                            effective_capacity = sum(min(g['mw'] + 20, g['rated']) for g in gen_statuses if g['status'] == 'ONLINE')
                            true_reserve = effective_capacity - expected_on_demand
                            
                            for load in live_loads:
                                if load['name'] in shed_set:
                                    if load['max_mw'] <= true_reserve:
                                        shed_set.remove(load['name'])
                                        log_msg = f"RESTORASI: Menyalakan kembali {load['name']} (Maks {load['max_mw']} MW)"
                                        app.restore_timer = 0
                                        break # Hanya 1 per siklus
                        else:
                            log_msg = f"Menunggu stabilitas sistem... ({app.restore_timer}/10)"
                    else:
                        app.restore_timer = 0
                        if not is_settled:
                            log_msg = "Menunggu beban/generator mencapai setpoint (Soft-Start)..."
                        else:
                            log_msg = "Menunggu frekuensi stabil di 49.95 - 50.05 Hz untuk restorasi..."

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
        vals = [max(0, min(32767, int(v))) for v in vals]
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
        override_info = {f'%MW{40+i}={v}' for i, v in enumerate(vals) if v > 0}
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