import time
import random
import math
import os
from pymodbus.client import ModbusTcpClient

# =========================================================
# KONFIGURASI PLC
# =========================================================
plc_ip = os.getenv('PLC_IP', '192.168.100.195')
plc_port = int(os.getenv('PLC_PORT', 502))
client = ModbusTcpClient(plc_ip, port=plc_port)
client.connect()

# =========================================================
# PETA MEMORY WORD
# =========================================================
# %MW0–%MW11  : Nilai beban aktual (ditulis load.py)
# %MW20–%MW21 : Jam & Menit simulasi
# %MW30–%MW33 : Output generator PLTA/PLTS/PLTGU/PLTB (MW)
# %MW40–%MW51 : Override beban per-channel (0=AUTO, >0=CONFIG)
# %MW54–%MW57 : Frekuensi sistem × 100 (5000 = 50.00 Hz)
#               ditulis load.py, dibaca app.py/HMI

ADDR_LOADS    = 0
ADDR_CLOCK    = 20
ADDR_GEN_OUT  = 30   # %MW30–%MW33
ADDR_OVERRIDE = 40   # %MW40–%MW51
ADDR_FREQ     = 54   # %MW54 (frekuensi × 100, integer)

# Coils
GEN_COIL_CMD = {'PLTA': 50, 'PLTS': 51, 'PLTGU': 52, 'PLTB': 53}
GEN_COIL_STS = {'PLTA': 0, 'PLTS': 1, 'PLTGU': 2, 'PLTB': 3}

LOAD_COILS = {
    'L101': 21, 'L201': 22, 'L303': 23, 'L304': 24, 'L305': 25, 'L401': 26,
    'L301': 31, 'L302': 32, 'L402': 33,
    'L403': 41, 'L404': 42, 'L405': 43
}

# =========================================================
# KONFIGURASI BEBAN
# =========================================================
load_names = ['L101','L201','L303','L304','L305','L401','L301','L302','L402','L403','L404','L405']
max_vals   = [ 20,    20,    15,    20,    30,    30,    5,     10,    5,     10,    15,    20  ]

# =========================================================
# KONFIGURASI GENERATOR — Karakteristik Fisik Per Jenis
# =========================================================
GEN_CFG = {
    'PLTA': {
        'type': 'hydro',
        'rated': 75.0, 'min': 5.0,
        'H': 5.0,       # Tinggi
        'droop': 0.05,
        'ramp': 1.5,    # REALISTIS: ~1.0 MW per detik (Air butuh waktu membuka katup penstock)
        'mw_addr': 30,
    },
    'PLTS': {
        'type': 'solar_bess', # PLTS + BESS
        'rated': 25.0, 'min': 0.0,
        'H': 0.5,       # BESS virtual inertia
        'droop': 0.02,
        'ramp': 5.0,    # BESS SANGAT CEPAT: ~20.0 MW per detik (Baterai inverter merespons instan)
        'mw_addr': 31,
    },
    'PLTGU': {
        'type': 'gas',
        'rated': 75.0, 'min': 15.0,
        'H': 4.0,       # Sedang
        'droop': 0.04,
        'ramp': 1.5,    # REALISTIS: ~1.0 MW per detik (Turbin gas/uap butuh waktu spooling)
        'mw_addr': 32,
    },
    'PLTB': {
        'type': 'wind',
        'rated': 25.0, 'min': 0.0,
        'H': 1.0,       # DFIG inertia
        'droop': 0.04,
        'ramp': 2.0,    # REALISTIS: ~4.0 MW per detik (Pitch control baling-baling)
        'mw_addr': 33,
    },
}

GEN_ORDER = ['PLTA', 'PLTS', 'PLTGU', 'PLTB']

# =========================================================
# PROFIL BEBAN HARIAN (WBP/LWBP Indonesia)
# =========================================================
def get_load_pct(hour):
    profile = {0:0.41, 4:0.32, 6:0.59, 9:0.72,
               12:0.77, 15:0.72, 17:0.81, 19:0.90,
               21:0.77, 23:0.54, 24:0.41}
    hs = sorted(profile.keys())
    for i in range(len(hs)-1):
        if hs[i] <= hour <= hs[i+1]:
            h1,h2,p1,p2 = hs[i],hs[i+1],profile[hs[i]],profile[hs[i+1]]
            return p1 if h2==h1 else p1+((hour-h1)/(h2-h1))*(p2-p1)
    return 0.45

# =========================================================
# PROFIL SOLAR (PLTS) — Mengikuti Iradiasi Matahari
# =========================================================
def get_solar_factor(hour):
    """0.0–1.0 berdasarkan posisi matahari (sinus) + variasi awan"""
    if hour < 5.5 or hour > 18.5:
        return 0.0
    peak = math.sin(math.pi * (hour - 5.5) / 13.0)
    cloud = random.uniform(0.75, 1.0)   # simulasi awan acak
    return max(0.0, peak * cloud)

# =========================================================
# INISIALISASI STATE
# =========================================================
sim_time = 19.0
init_pct = get_load_pct(sim_time % 24)
current_loads = [max(1.0, float(m * init_pct)) for m in max_vals]

# RESET PLC OVERRIDES & COILS SAAT STARTUP — tunggu sampai koneksi berhasil
print("Membersihkan memori PLC (Override & Breaker)...")
while True:
    try:
        if client.connect():
            client.write_registers(address=ADDR_OVERRIDE, values=[0]*12)
            for name in load_names:
                client.write_coil(LOAD_COILS[name], False)
            print("PLC berhasil dibersihkan. Simulator siap!")
            break
        else:
            print("Menunggu koneksi PLC untuk inisialisasi...")
    except Exception as e:
        print(f"Menunggu PLC... ({e})")
    import time as _t; _t.sleep(2)

init_total = sum(current_loads)

# State output generator saat ini (MW) - Dinamis mengikuti beban awal agar tidak trip di detik pertama
gen_output = {
    'PLTA':  (75.0 / 200.0) * init_total,
    'PLTS':  (25.0 / 200.0) * init_total,
    'PLTGU': (75.0 / 200.0) * init_total,
    'PLTB':  (25.0 / 200.0) * init_total,
}

# Frekuensi sistem (Hz)
freq = 50.0

F_NOM   = 50.0    # Frekuensi nominal (Hz)
S_BASE  = sum(GEN_CFG[g]['rated'] for g in GEN_ORDER)  # Total kapasitas (MVA)

print("=" * 65)
print("  SIMULATOR — Beban & Generator → PLC")
gen_summary = ', '.join(f"{g}({GEN_CFG[g]['type']})" for g in GEN_ORDER)
print(f"  Generator: {gen_summary}")
print(f"  S_base = {S_BASE:.0f} MVA | F_nom = {F_NOM} Hz")
print("=" * 65)

# =========================================================
# LOOP UTAMA
# =========================================================
DT = 0.1   # Refresh rate 10x per detik (0.1s)

while True:
    try:
        hour = sim_time % 24

        # ─────────────────────────────────────────────
        # 1. BACA STATUS BREAKER (Generator & Beban)
        # ─────────────────────────────────────────────
        gen_online = {}
        for g in GEN_ORDER:
            cmd = client.read_coils(GEN_COIL_CMD[g], count=1)
            is_off_cmd = cmd.bits[0] if not cmd.isError() else False
            gen_online[g] = not is_off_cmd
            client.write_coil(GEN_COIL_STS[g], gen_online[g])

        load_tripped = {}
        for name in load_names:
            c = client.read_coils(LOAD_COILS[name], count=1)
            load_tripped[name] = c.bits[0] if not c.isError() else False

        # ─────────────────────────────────────────────
        # 2. UPDATE BEBAN (dengan per-channel override & trip)
        # ─────────────────────────────────────────────
        ov_result = client.read_holding_registers(address=ADDR_OVERRIDE, count=12)
        override  = list(ov_result.registers) if not ov_result.isError() else [0]*12

        target_pct = get_load_pct(hour)
        override_ch = []
        for i, name in enumerate(load_names):
            is_overridden = False
            if override[i] > 0:
                potential = min(override[i], max_vals[i])
                override_ch.append(name)
                is_overridden = True
            else:
                tgt = max_vals[i] * target_pct
            
            # Jika tripped (coil == True), actual load = 0
            if load_tripped[name]:
                current_loads[i] = 0.0
                final_val = 0.0
            elif is_overridden:
                # Bypass soft-start: Langsung statis ke angka override
                current_loads[i] = float(potential)
                final_val = current_loads[i]
            else:
                # Soft-start base load agar tidak melonjak kasar
                diff = tgt - current_loads[i]
                max_step = max_vals[i] * 0.01 
                move = max(-max_step, min(max_step, diff))
                current_loads[i] += move
                
                # Tambahkan noise dinamis agar terlihat real-time (hanya jika sudah dekat target)
                if abs(diff) < max_vals[i] * 0.05:
                    noise = random.uniform(-0.03, 0.03) * max_vals[i]
                else:
                    noise = 0.0
                    
                final_val = current_loads[i] + noise
                final_val = max(1.0, min(float(max_vals[i]), final_val))
                
            # Tulis nilai aktual ke %MW0--%MW11
            client.write_register(address=ADDR_LOADS + i, value=int(round(final_val)))
            
        # Hitung total load dari array current_loads yang murni tanpa noise
        # Ini penting agar generator dispatch tidak ikut bergetar akibat noise.
        total_load = sum(current_loads)

        # ─────────────────────────────────────────────
        # 3. DISPATCH GENERATOR — BESS membuat PLTS dispatchable
        # ─────────────────────────────────────────────
        required_dispatch = total_load
        
        avail_plta  = GEN_CFG['PLTA']['rated'] if gen_online['PLTA'] else 0.0
        avail_plts  = GEN_CFG['PLTS']['rated'] if gen_online['PLTS'] else 0.0
        avail_pltgu = GEN_CFG['PLTGU']['rated'] if gen_online['PLTGU'] else 0.0
        avail_pltb  = GEN_CFG['PLTB']['rated'] if gen_online['PLTB'] else 0.0
        
        total_avail = avail_plta + avail_plts + avail_pltgu + avail_pltb

        targets = {}
        if total_avail > 0:
            targets['PLTA']  = (avail_plta / total_avail) * required_dispatch if gen_online['PLTA'] else 0.0
            targets['PLTS']  = (avail_plts / total_avail) * required_dispatch if gen_online['PLTS'] else 0.0
            targets['PLTGU'] = (avail_pltgu / total_avail) * required_dispatch if gen_online['PLTGU'] else 0.0
            targets['PLTB']  = (avail_pltb / total_avail) * required_dispatch if gen_online['PLTB'] else 0.0
        else:
            targets['PLTA'] = targets['PLTS'] = targets['PLTGU'] = targets['PLTB'] = 0.0

        for g in GEN_ORDER:
            if not gen_online[g]:
                gen_output[g] = 0.0
                continue
                
            target = targets[g]
            # Primary frequency response (Droop)
            if GEN_CFG[g]['droop'] > 0:
                dp = - ((freq - 50.0) / 50.0) * (1.0 / GEN_CFG[g]['droop']) * GEN_CFG[g]['rated']
                target += dp
            target = max(GEN_CFG[g]['min'], min(GEN_CFG[g]['rated'], target))
            
            # Ramp Rate - BESS / Inverters have extremely fast ramp rates
            diff_g = target - gen_output[g]
            ramp_multiplier = 4.0 if GEN_CFG[g]['type'] in ['solar_bess', 'wind'] else 2.0
            ramp_limit = GEN_CFG[g]['ramp'] * ramp_multiplier * DT
            step_g = max(-ramp_limit, min(ramp_limit, diff_g))
            
            noise_g = random.uniform(-0.1, 0.1) if target > 0 else 0.0
            new_out = gen_output[g] + step_g + noise_g
            
            gen_output[g] = max(GEN_CFG[g]['min'], min(GEN_CFG[g]['rated'], round(new_out, 3)))

        total_gen = sum(gen_output.values())

        # ─────────────────────────────────────────────
        # 4. HITUNG FREKUENSI SISTEM (Swing Equation)
        # ─────────────────────────────────────────────
        delta_p_pu = (total_gen - total_load) / S_BASE

        h_weighted = sum(GEN_CFG[g]['H'] * gen_output[g] for g in GEN_ORDER if gen_online[g])
        h_eff = h_weighted / max(total_gen, 1.0)
        
        # Penambahan Virtual Inertia untuk menstabilkan simulasi
        h_eff += 2.0 
        
        # Load Damping Factor (D) - Beban ikut turun konsumsinya saat frekuensi turun
        D = 1.5
        damping_p = D * ((freq - F_NOM) / F_NOM)

        if total_avail > 0:
            # SISTEM NORMAL / TERHUBUNG: Gunakan persamaan swing
            if h_eff > 0.1:
                dfdt = (F_NOM / (2.0 * h_eff)) * (delta_p_pu - damping_p)
            else:
                dfdt = (delta_p_pu - damping_p) * 2.0
                
            freq += dfdt * DT
            
            # AGC dan Recovery frekuensi sekarang sepenuhnya ditangani secara FISIKA NYATA
            # oleh algoritma Dispatch (required_dispatch = total_load) dan Droop Control
            # pada generator, sehingga modifikasi matematis buatan di sini telah dihapus.
                
            # CLAMP FREQUENCY (Realistic)
            # Diperlebar (45-55 Hz) agar jika frekuensi anjlok ke 47 Hz, sistem proteksi 
            # (UFR / Load Shedding) masih berkesempatan bekerja sebelum frekuensi mentok.
            freq = max(45.0, min(55.0, freq))
            rocof = dfdt
            
        else:
            # SISTEM BLACKOUT (Semua generator trip)
            # Generator terlepas dari jaringan, sehingga perlambatannya adalah karena gesekan mekanik
            # turbin (Spin down / coast-down). Perlambatan meluruh secara eksponensial.
            dfdt = - (freq / 3.0)  # Perlambatan natural turbin (time constant ~3s)
            freq += dfdt * DT
            if freq < 0.1:
                freq = 0.0
                dfdt = 0.0
                
            rocof = dfdt

        # ─────────────────────────────────────────────
        # 5. TULIS KE PLC
        # ─────────────────────────────────────────────
        # Beban → %MW0–%MW11
        client.write_registers(address=ADDR_LOADS, values=[int(round(c)) for c in current_loads])

        # Generator output → %MW30–%MW33 (integer MW)
        gen_vals = [int(round(gen_output[g])) for g in GEN_ORDER]
        client.write_registers(address=ADDR_GEN_OUT, values=gen_vals)

        # Jam simulasi → %MW20–%MW21
        h_i = int(hour)
        m_i = int((hour - h_i) * 60)
        client.write_registers(address=ADDR_CLOCK, values=[h_i, m_i])

        # Frekuensi → %MW54 (× 100 agar integer: 5000 = 50.00 Hz)
        freq_int = int(round(freq * 100))
        client.write_register(address=ADDR_FREQ, value=freq_int)

        s_i = int((((hour - h_i) * 60) - m_i) * 60)
        # ─────────────────────────────────────────────
        # 5. LOG TERMINAL
        # ─────────────────────────────────────────────
        ov_str = f" | OV:{override_ch}" if override_ch else ""
        print(
            f"[{h_i:02d}:{m_i:02d}:{s_i:02d}] "
            f"LOAD:{total_load:.1f}MW | "
            f"GEN:{total_gen:.0f}MW "
            f"(PLTA:{gen_output['PLTA']:.0f} "
            f"PLTS:{gen_output['PLTS']:.0f} "
            f"PLTGU:{gen_output['PLTGU']:.0f} "
            f"PLTB:{gen_output['PLTB']:.0f}) | "
            f"f={freq:.3f}Hz RoCoF={rocof:+.3f}Hz/s{ov_str}"
        )

    except Exception as e:
        print(f"[ERROR] {e}")
        try:
            client.connect()
        except Exception:
            pass

    time.sleep(DT)
    # 5 detik di dunia nyata = 1 menit di simulasi
    # sim_time dalam jam, maka 1 menit = 1/60 jam.
    # Karena loop berjalan setiap DT detik, dalam 5 detik ada 5/DT siklus.
    # Kenaikan per siklus = (1/60) / (5/DT) = DT / 300.0
    sim_time += DT / 60.0