"""
load.py - Physics engine beban & generator (swing equation) -> PLC M241
=======================================================================
Setiap siklus (DT = 0.1 s):
  1. Baca perintah trip generator & beban dari PLC (1 transaksi Modbus)
  2. Hitung beban (profil harian, soft-start, override HMI, noise)
  3. Dispatch generator + governor droop + ramp rate
  4. Hitung frekuensi (swing equation)
  5. Tulis hasil ke PLC (beban, generator, jam, frekuensi)
"""
import os
import random
import time

from plc_modbus import M241Client as ModbusTcpClient

# =========================================================
# KONFIGURASI PLC
# =========================================================
plc_ip = os.getenv('PLC_IP', '192.168.100.200')
plc_port = int(os.getenv('PLC_PORT', 502))
client = ModbusTcpClient(plc_ip, port=plc_port)
client.connect()

# =========================================================
# PETA MEMORY WORD
# =========================================================
# %MW0-%MW11  : Nilai beban aktual (ditulis load.py)
# %MW20-%MW21 : Jam & Menit simulasi
# %MW30-%MW33 : Output generator PLTA/PLTS/PLTGU/PLTB (MW x10)
# %MW40-%MW51 : Override beban per-channel (0=AUTO, >0=CONFIG)
# %MW54       : Frekuensi sistem x100 (5000 = 50.00 Hz)
ADDR_LOADS    = 0
ADDR_CLOCK    = 20
ADDR_GEN_OUT  = 30
ADDR_OVERRIDE = 40
ADDR_FREQ     = 54

# Bit %M (lewat adapter plc_modbus)
GEN_COIL_CMD = {'PLTA': 50, 'PLTS': 51, 'PLTGU': 52, 'PLTB': 53}   # perintah trip dari SCADA
GEN_COIL_STS = {'PLTA': 0, 'PLTS': 1, 'PLTGU': 2, 'PLTB': 3}       # status ONLINE ke SCADA
LOAD_COILS = {
    'L101': 21, 'L201': 22, 'L303': 23, 'L304': 24, 'L305': 25, 'L401': 26,
    'L301': 31, 'L302': 32, 'L402': 33,
    'L403': 41, 'L404': 42, 'L405': 43,
}
# Satu blok baca mencakup bit trip beban (21-43) dan perintah generator (50-53)
CMD_BIT_FIRST = 21
CMD_BIT_COUNT = 53 - CMD_BIT_FIRST + 1

# =========================================================
# KONFIGURASI BEBAN
# =========================================================
load_names = ['L101', 'L201', 'L303', 'L304', 'L305', 'L401', 'L301', 'L302', 'L402', 'L403', 'L404', 'L405']
max_vals   = [20,     20,     15,     20,     30,     30,     5,      10,     5,      10,     15,     20]

# =========================================================
# KONFIGURASI GENERATOR - Karakteristik Fisik Per Jenis
# =========================================================
GEN_CFG = {
    'PLTA':  {'type': 'hydro',      'rated': 75.0, 'min': 5.0,  'H': 5.0, 'droop': 0.05, 'ramp': 5.0},
    'PLTS':  {'type': 'solar_bess', 'rated': 25.0, 'min': 0.0,  'H': 0.5, 'droop': 0.02, 'ramp': 5.0},  # PLTS + BESS (virtual inertia)
    'PLTGU': {'type': 'gas',        'rated': 75.0, 'min': 15.0, 'H': 4.0, 'droop': 0.04, 'ramp': 3.0},
    'PLTB':  {'type': 'wind',       'rated': 25.0, 'min': 0.0,  'H': 1.0, 'droop': 0.04, 'ramp': 2.0},  # DFIG
}
GEN_ORDER = ['PLTA', 'PLTS', 'PLTGU', 'PLTB']

F_NOM  = 50.0                                         # Frekuensi nominal (Hz)
S_BASE = sum(GEN_CFG[g]['rated'] for g in GEN_ORDER)  # Total kapasitas (MVA)
D_DAMPING = 1.5      # Load damping factor
H_VIRTUAL = 2.5        # Virtual inertia BESS grid-forming (jaga nadir > 49.50 Hz saat kontingensi ganda)
DT = 0.1             # Siklus 10x per detik
TIME_SPEED_FACTOR = 6.0    # 6.0x: 10 detik dunia nyata = 1 menit simulasi


# =========================================================
# PROFIL BEBAN HARIAN (WBP/LWBP Indonesia)
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
# INISIALISASI STATE
# =========================================================
sim_time = 19.0
init_pct = get_load_pct(sim_time % 24)
current_loads = [max(1.0, float(m * init_pct)) for m in max_vals]
load_noise_factors = [0.0] * 12

# Reset override & breaker beban di PLC saat startup - tunggu sampai koneksi berhasil
print("Membersihkan memori PLC (Override & Breaker)...")
while True:
    try:
        if client.connect():
            client.write_registers(address=ADDR_OVERRIDE, values=[0] * 12)
            client.write_coil_map({LOAD_COILS[n]: False for n in load_names})
            print("PLC berhasil dibersihkan. Simulator siap!")
            break
        print("Menunggu koneksi PLC untuk inisialisasi...")
    except Exception as e:
        print(f"Menunggu PLC... ({e})")
    time.sleep(2)

# Output generator awal mengikuti beban awal agar tidak trip di detik pertama
init_total = sum(current_loads)
gen_output = {g: (GEN_CFG[g]['rated'] / S_BASE) * init_total for g in GEN_ORDER}
freq = 50.0

print("=" * 65)
print("  SIMULATOR - Beban & Generator -> PLC")
gen_summary = ', '.join(f"{g}({GEN_CFG[g]['type']})" for g in GEN_ORDER)
print(f"  Generator: {gen_summary}")
print(f"  S_base = {S_BASE:.0f} MVA | F_nom = {F_NOM} Hz")
print("=" * 65)

# =========================================================
# LOOP UTAMA
# =========================================================
while True:
    try:
        hour = sim_time % 24

        # ── 1. BACA PERINTAH TRIP (generator & beban) - satu transaksi
        cmd = client.read_coils(CMD_BIT_FIRST, count=CMD_BIT_COUNT)
        bits = cmd.bits if not cmd.isError() else [False] * CMD_BIT_COUNT
        gen_online = {g: not bits[GEN_COIL_CMD[g] - CMD_BIT_FIRST] for g in GEN_ORDER}
        load_tripped = {n: bits[LOAD_COILS[n] - CMD_BIT_FIRST] for n in load_names}
        client.write_coil_map({GEN_COIL_STS[g]: gen_online[g] for g in GEN_ORDER})

        # ── 2. UPDATE BEBAN (override per-channel, trip, soft-start, noise)
        ov_result = client.read_holding_registers(address=ADDR_OVERRIDE, count=12)
        override = list(ov_result.registers) if not ov_result.isError() else [0] * 12

        target_pct = get_load_pct(hour)
        override_ch = []
        load_regs = []
        total_load_with_noise = 0.0
        for i, name in enumerate(load_names):
            if override[i] > 0:
                override_ch.append(name)
            if load_tripped[name]:
                current_loads[i] = 0.0
                final_val = 0.0
            elif override[i] > 0:
                # Override HMI: langsung ke nilai paksa (tanpa soft-start)
                current_loads[i] = float(min(override[i] / 10.0, max_vals[i]))
                final_val = current_loads[i]
            else:
                # Soft-start menuju target profil harian
                diff = max_vals[i] * target_pct - current_loads[i]
                max_step = max_vals[i] * 0.01
                current_loads[i] += max(-max_step, min(max_step, diff))

                # Noise dinamis mengayun halus per siklus (rentang fluktuasi ±5%)
                if abs(diff) < max_vals[i] * 0.05:
                    load_noise_factors[i] += random.uniform(-0.002, 0.002)
                    load_noise_factors[i] = max(-0.05, min(0.05, load_noise_factors[i]))
                    noise = load_noise_factors[i] * max_vals[i]
                else:
                    noise = 0.0
                final_val = max(1.0, min(float(max_vals[i]), current_loads[i] + noise))

            total_load_with_noise += final_val
            load_regs.append(int(round(final_val * 10)))   # x10 -> resolusi 0.1 MW

        # Dispatch memakai beban tanpa noise agar generator tidak ikut bergetar
        total_load = sum(current_loads)

        # ── 3. DISPATCH GENERATOR (BESS membuat PLTS dispatchable)
        avail = {g: GEN_CFG[g]['rated'] if gen_online[g] else 0.0 for g in GEN_ORDER}
        total_avail = sum(avail[g] for g in GEN_ORDER)

        for g in GEN_ORDER:
            cfg = GEN_CFG[g]
            if not gen_online[g]:
                gen_output[g] = 0.0
                continue
            target = (avail[g] / total_avail) * total_load
            # Primary frequency response (droop)
            if cfg['droop'] > 0:
                target += -((freq - 50.0) / 50.0) * (1.0 / cfg['droop']) * cfg['rated']
            target = max(cfg['min'], min(cfg['rated'], target))

            # Ramp rate (inverter BESS/angin lebih cepat)
            ramp_multiplier = 4.0 if cfg['type'] in ('solar_bess', 'wind') else 2.0
            ramp_limit = cfg['ramp'] * ramp_multiplier * DT
            step_g = max(-ramp_limit, min(ramp_limit, target - gen_output[g]))
            noise_g = random.uniform(-0.1, 0.1) if target > 0 else 0.0
            gen_output[g] = max(cfg['min'], min(cfg['rated'], round(gen_output[g] + step_g + noise_g, 3)))

        total_gen = sum(gen_output.values())

        # ── 4. FREKUENSI SISTEM (swing equation)
        if total_avail > 0:
            delta_p_pu = (total_gen - total_load_with_noise) / S_BASE
            h_eff = sum(GEN_CFG[g]['H'] * gen_output[g] for g in GEN_ORDER if gen_online[g]) / max(total_gen, 1.0)
            h_eff += H_VIRTUAL
            damping_p = D_DAMPING * ((freq - F_NOM) / F_NOM)
            if h_eff > 0.1:
                dfdt = (F_NOM / (2.0 * h_eff)) * (delta_p_pu - damping_p)
            else:
                dfdt = (delta_p_pu - damping_p) * 2.0
            # Recovery ditangani fisika (dispatch + droop). Clamp 45-55 Hz agar UFLS
            # masih sempat bekerja sebelum frekuensi mentok.
            freq = max(45.0, min(55.0, freq + dfdt * DT))
        else:
            # BLACKOUT: turbin coast-down (time constant ~3 s)
            dfdt = -(freq / 3.0)
            freq += dfdt * DT
            if freq < 0.1:
                freq, dfdt = 0.0, 0.0
        rocof = dfdt

        # ── 5. TULIS KE PLC
        gen_vals = [int(round(gen_output[g] * 10)) for g in GEN_ORDER]
        h_i = int(hour)
        m_i = int((hour - h_i) * 60)
        s_i = int((((hour - h_i) * 60) - m_i) * 60)
        client.write_registers(address=ADDR_LOADS, values=load_regs)     # %MW0-11
        client.write_registers(address=ADDR_CLOCK, values=[h_i, m_i])    # %MW20-21
        client.write_registers(address=ADDR_GEN_OUT, values=gen_vals)    # %MW30-33
        client.write_register(address=ADDR_FREQ, value=int(round(freq * 100)))  # %MW54

        # ── 6. LOG TERMINAL
        ov_str = f" | OV:{override_ch}" if override_ch else ""
        print(
            f"[{h_i:02d}:{m_i:02d}:{s_i:02d}] "
            f"LOAD:{sum(load_regs) / 10.0:.1f}MW | "
            f"GEN:{sum(gen_vals) / 10.0:.1f}MW "
            f"(PLTA:{gen_output['PLTA']:.1f} PLTS:{gen_output['PLTS']:.1f} "
            f"PLTGU:{gen_output['PLTGU']:.1f} PLTB:{gen_output['PLTB']:.1f}) | "
            f"f={freq:.3f}Hz RoCoF={rocof:+.3f}Hz/s{ov_str}"
        )

    except Exception as e:
        print(f"[ERROR] {e}")
        try:
            client.connect()
        except Exception:
            pass

    time.sleep(DT)
    sim_time += (DT * TIME_SPEED_FACTOR) / 3600.0   # 6.0x: 10 s nyata = 1 menit simulasi
