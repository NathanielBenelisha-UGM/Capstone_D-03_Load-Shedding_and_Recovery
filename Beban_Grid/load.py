"""
load.py - Physics engine beban & generator (swing equation) -> PLC M241
=======================================================================
Setiap siklus (target DT = 0.1 s, fisika memakai waktu nyata terukur dt
dalam sub-langkah <= 10 ms):
  1. Baca perintah trip generator & beban dari PLC (1 transaksi Modbus)
  2. Hitung beban (profil harian, soft-start, override HMI, noise)
  3. Dispatch generator + governor droop + ramp rate  } diulang per
  4. Hitung frekuensi (swing equation)                } sub-langkah
  5. Tulis hasil ke PLC (beban, generator, jam, frekuensi)
"""
import math
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

# Pencatat IO (hanya mencatat, tidak mengubah fisika): waktu Modbus / print / fisika per siklus.
# Dicetak sebagai [IO load.py] hanya bila kerja satu siklus > IO_SLOW_MS (normal 30-40 ms).
IO_SLOW_MS = 120.0
_io = {'n': 0, 'ms': 0.0, 'max': 0.0}
_io_execute = client.execute


def _io_counting_execute(*args, **kwargs):
    t0 = time.perf_counter()
    try:
        return _io_execute(*args, **kwargs)
    finally:
        ms = (time.perf_counter() - t0) * 1000.0
        _io['n'] += 1
        _io['ms'] += ms
        _io['max'] = max(_io['max'], ms)


client.execute = _io_counting_execute

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
# KONFIGURASI GENERATOR - Karakteristik Fisik Dunia Nyata
# =========================================================
# ramp  = laju setpoint dispatch (MW/s, load following / AGC)
# pfr   = pita respons primer governor (+/- MW), T_gov = konstanta waktu governor+turbin (s)
#         -> respons droop TIDAK dibatasi ramp, tetapi oleh pita & lag ini (nilai tipikal, perlu referensi)
GEN_CFG = {
    # Hydro: min 20%, H=3.5s, droop=5%, ramp=0.20 MW/s (12 MW/menit), primer 10% / 4 s
    'PLTA':  {'type': 'hydro',      'rated': 75.0, 'min': 15.0, 'H': 3.5, 'droop': 0.05, 'ramp': 0.20, 'pfr': 7.5,  'T_gov': 4.0},
    # Solar+BESS: min 0%, H=1.5s (VSG), droop=2% (FFR), ramp=12.5 MW/s (inverter baterai cepat), primer penuh / 0.2 s
    'PLTS':  {'type': 'solar_bess', 'rated': 25.0, 'min': 0.0,  'H': 1.5, 'droop': 0.02, 'ramp': 12.5, 'pfr': 25.0, 'T_gov': 0.2},
    # CCGT: min 40%, H=4.5s, droop=4%, ramp=0.08 MW/s (4.8 MW/menit), primer ~7% / 3 s (porsi turbin gas)
    'PLTGU': {'type': 'gas',        'rated': 75.0, 'min': 30.0, 'H': 4.5, 'droop': 0.04, 'ramp': 0.08, 'pfr': 5.0,  'T_gov': 3.0},
    # Wind DFIG: min 1MW, H=1.5s, droop=4%, ramp=0.06 MW/s (3.6 MW/menit), primer 5% / 1 s (kontrol pitch/deloading)
    'PLTB':  {'type': 'wind',       'rated': 25.0, 'min': 1.0,  'H': 1.5, 'droop': 0.04, 'ramp': 0.06, 'pfr': 1.25, 'T_gov': 1.0},
}
GEN_ORDER = ['PLTA', 'PLTS', 'PLTGU', 'PLTB']

F_NOM  = 50.0                                         # Frekuensi nominal (Hz)
S_BASE = sum(GEN_CFG[g]['rated'] for g in GEN_ORDER)  # Total kapasitas (MVA) = 200 MVA
D_DAMPING = 1.5                                       # Load damping factor
H_VIRTUAL = 0.0                                       # Inersia BESS sudah dimodelkan di PLTS (H=1.5s)
DT = 0.1                                              # Siklus 10x per detik (100 ms)
DT_SUB = 0.01                                         # Sub-langkah integrasi fisika (10 ms)
DT_MAX = 0.3                                          # Batas dt per siklus (mis. setelah koneksi PLC putus)
TIME_SPEED_FACTOR = 1.0                               # 1.0x: Waktu nyata murni (1s nyata = 1s simulasi)

# AGC / kontrol sekunder satu area (nilai tipikal sistem kecil/terisolasi, perlu referensi):
# bias += AGC_BETA * (50 - f) / AGC_T * dt  -> offset frekuensi hilang dalam orde AGC_T
AGC_BETA = 100.0      # MW/Hz, frequency bias ~ kekakuan sistem (droop semua unit + damping beban ~ 111 MW/Hz)
AGC_T = 60.0          # s, konstanta waktu kontrol sekunder
AGC_BAND_HZ = 0.5     # |f - 50| > ini: integrasi DITAHAN (gangguan besar ditangani shedding + respons primer)
AGC_LIMIT_MW = 20.0   # batas bias (10% S_base)


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

# Hemat transaksi Modbus: %MW0-%MW33 dikirim dalam SATU transaksi FC23 (tulis %MW0-33 + baca override
# %MW40-51). Isi %MW12-19 dan %MW22-29 (tidak dipakai SCADA/ladder) dibaca sekali di sini lalu ditulis
# ulang apa adanya. Bila PLC menolak FC23, otomatis kembali ke tulis terpisah seperti sebelumnya.
_fill = client.read_holding_registers(address=0, count=ADDR_GEN_OUT + 4)
fill_regs = list(_fill.registers) if not _fill.isError() else [0] * (ADDR_GEN_OUT + 4)
use_fc23 = True
override = [0] * 12
# Status generator (%M0-3) hanya ditulis saat berubah, plus penyegaran tiap GEN_STS_REFRESH_S
GEN_STS_REFRESH_S = 1.0
gen_sts_written, gen_sts_t = None, 0.0

# Output generator awal mengikuti beban awal agar tidak trip di detik pertama
init_total = sum(current_loads)
gen_output = {g: (GEN_CFG[g]['rated'] / S_BASE) * init_total for g in GEN_ORDER}
gen_base = dict(gen_output)              # setpoint dispatch (bergerak dengan ramp)
gen_pfr = {g: 0.0 for g in GEN_ORDER}    # respons primer governor (lag T_gov, pita +/- pfr)
freq = 50.0
agc_bias = 0.0

print("=" * 65)
print("  SIMULATOR - Beban & Generator -> PLC")
gen_summary = ', '.join(f"{g}({GEN_CFG[g]['type']})" for g in GEN_ORDER)
print(f"  Generator: {gen_summary}")
print(f"  S_base = {S_BASE:.0f} MVA | F_nom = {F_NOM} Hz")
print("=" * 65)


# =========================================================
# PENGUKURAN WAKTU SIKLUS NYATA
# =========================================================
class CycleMeter:
    """Mengukur waktu siklus nyata (proses + sleep), ringkasan dicetak tiap `report_s` detik.

    start() di awal siklus, work_done() tepat sebelum sleep, end() setelah sleep.
    Siklus yang tidak sampai end() (mis. menunggu koneksi PLC) tidak dihitung.
    Faktor waktu = nominal / siklus nyata (1.00x = sesuai asumsi DT).
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


cycle_meter = CycleMeter('load.py', DT)   # jadwal target DT; fisika memakai dt terukur


# =========================================================
# FISIKA PER SUB-LANGKAH (AGC + setpoint/ramp + governor primer + swing equation)
# =========================================================
def physics_substep(h, gen_online, total_load, total_load_with_noise):
    """Majukan fisika sebesar h detik. Return df/dt (Hz/s)."""
    global freq, agc_bias

    # ── 3. DISPATCH GENERATOR (BESS membuat PLTS dispatchable)
    online = [g for g in GEN_ORDER if gen_online[g]]
    for g in GEN_ORDER:
        if not gen_online[g]:
            gen_output[g] = gen_base[g] = gen_pfr[g] = 0.0

    # Secondary Frequency Control (AGC): integral frequency bias -> offset frekuensi ke 50.00 Hz
    if online:
        df = F_NOM - freq
        sum_base = sum(gen_base[g] for g in online)
        saturated_up = sum_base >= sum(GEN_CFG[g]['rated'] for g in online) - 0.1
        saturated_dn = sum_base <= sum(GEN_CFG[g]['min'] for g in online) + 0.1
        # Anti-windup: integrasi ditahan saat gangguan besar atau armada sudah di batas fisiknya
        if abs(df) <= AGC_BAND_HZ and not (df > 0 and saturated_up) and not (df < 0 and saturated_dn):
            agc_bias += AGC_BETA * df / AGC_T * h
            agc_bias = max(-AGC_LIMIT_MW, min(AGC_LIMIT_MW, agc_bias))
    else:
        agc_bias = 0.0
    adjusted_load = max(0.0, total_load + agc_bias)

    # (a0) Transisi sinkronisasi naik (ramp-up halus) dari 0 MW ke batas teknis minimum
    synced = []
    for g in online:
        cfg = GEN_CFG[g]
        if gen_base[g] < cfg['min']:
            sync_ramp = max(cfg['ramp'], cfg['min'] / 15.0) * h
            gen_base[g] = min(cfg['min'], gen_base[g] + sync_ramp)
            gen_pfr[g] = 0.0
            gen_output[g] = gen_base[g]
        else:
            synced.append(g)

    # (a) Setpoint dispatch SEIMBANG, bergerak dengan ramp rate (MW/s). Penurunan (de-loading/runback saat
    #     over-frequency) 3x lebih cepat. Unit yang sedang sinkron ikut dihitung di total setpoint.
    if synced:
        runback_mult = 3.0 if freq > 50.05 else 1.0
        cap_up = {g: max(0.0, min(GEN_CFG[g]['ramp'] * h, GEN_CFG[g]['rated'] - gen_base[g])) for g in synced}
        cap_dn = {g: max(0.0, min(GEN_CFG[g]['ramp'] * runback_mult * h, gen_base[g] - GEN_CFG[g]['min']))
                  for g in synced}
        step = dict.fromkeys(synced, 0.0)

        # Tahap 1 - keseimbangan: total setpoint mengikuti beban + AGC secepat kemampuan ramp armada
        d_total = adjusted_load - sum(gen_base[g] for g in online)
        caps = cap_up if d_total > 0 else cap_dn
        sign = 1.0 if d_total > 0 else -1.0
        cap_sum = sum(caps.values())
        if cap_sum > 0:
            take = min(abs(d_total), cap_sum)
            for g in synced:
                step[g] = sign * take * caps[g] / cap_sum
                caps[g] -= abs(step[g])

        # Tahap 2 - perataan ke bagian ekonomis (rated / total rated) TANPA mengubah total setpoint:
        #           unit lama melepas beban hanya sebanyak yang diambil unit yang masih di bawah bagiannya
        synced_total = sum(gen_base[g] + step[g] for g in synced)
        rated_sum = sum(GEN_CFG[g]['rated'] for g in synced)
        gap = {g: max(GEN_CFG[g]['min'], min(GEN_CFG[g]['rated'], GEN_CFG[g]['rated'] / rated_sum * synced_total))
               - (gen_base[g] + step[g]) for g in synced}
        up = {g: min(cap_up[g], gap[g]) for g in synced if gap[g] > 1e-6}
        dn = {g: min(cap_dn[g], -gap[g]) for g in synced if gap[g] < -1e-6}
        r = min(sum(up.values()), sum(dn.values()))
        if r > 0:
            up_sum, dn_sum = sum(up.values()), sum(dn.values())
            for g, v in up.items():
                step[g] += r * v / up_sum
            for g, v in dn.items():
                step[g] -= r * v / dn_sum

        for g in synced:
            cfg = GEN_CFG[g]
            # Noise diskalakan sqrt(h/DT) agar besarnya per detik sama seperti 1 langkah 0.1 s
            noise_g = random.uniform(-0.005, 0.005) * (h / DT) ** 0.5
            gen_base[g] = max(cfg['min'], min(cfg['rated'], gen_base[g] + step[g] + noise_g))

    for g in synced:
        cfg = GEN_CFG[g]
        # (b) Respons primer governor (droop): lag orde-1 T_gov, dibatasi pita +/- pfr
        #     dan sisa ruang terhadap min/rated -> tidak dibatasi ramp setpoint
        pfr_target = -((freq - 50.0) / 50.0) * (1.0 / cfg['droop']) * cfg['rated'] if cfg['droop'] > 0 else 0.0
        pfr_target = max(-cfg['pfr'], min(cfg['pfr'], pfr_target))
        pfr_target = max(cfg['min'] - gen_base[g], min(cfg['rated'] - gen_base[g], pfr_target))
        gen_pfr[g] += (pfr_target - gen_pfr[g]) * min(1.0, h / cfg['T_gov'])

        gen_output[g] = max(cfg['min'], min(cfg['rated'], gen_base[g] + gen_pfr[g]))

    total_gen = sum(gen_output.values())

    # ── 4. FREKUENSI SISTEM (swing equation)
    if online:
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
        freq = max(45.0, min(55.0, freq + dfdt * h))
    else:
        # BLACKOUT: turbin coast-down (time constant ~3 s)
        dfdt = -(freq / 3.0)
        freq += dfdt * h
        if freq < 0.1:
            freq, dfdt = 0.0, 0.0
    return dfdt


# =========================================================
# LOOP UTAMA
# =========================================================
t_prev = time.perf_counter() - DT
while True:
    cycle_meter.start()
    # Waktu nyata sejak awal siklus sebelumnya -> langkah fisika siklus ini
    t_cycle = time.perf_counter()
    _io.update(n=0, ms=0.0, max=0.0)
    t_phys_ms = t_print_ms = 0.0
    dt_raw, t_prev = t_cycle - t_prev, t_cycle
    dt = min(DT_MAX, dt_raw)
    if dt_raw > DT_MAX:
        print(f"[DT] Siklus {dt_raw * 1000:.0f} ms > {DT_MAX * 1000:.0f} ms: fisika dibatasi {DT_MAX * 1000:.0f} ms")
    try:
        hour = sim_time % 24

        # ── 1. BACA PERINTAH TRIP (generator & beban) - satu transaksi
        cmd = client.read_coils(CMD_BIT_FIRST, count=CMD_BIT_COUNT)
        bits = cmd.bits if not cmd.isError() else [False] * CMD_BIT_COUNT
        gen_online = {g: not bits[GEN_COIL_CMD[g] - CMD_BIT_FIRST] for g in GEN_ORDER}
        load_tripped = {n: bits[LOAD_COILS[n] - CMD_BIT_FIRST] for n in load_names}
        if gen_online != gen_sts_written or t_cycle - gen_sts_t >= GEN_STS_REFRESH_S:
            sts = client.write_coil_map({GEN_COIL_STS[g]: gen_online[g] for g in GEN_ORDER})
            if sts is not None and not sts.isError():
                gen_sts_written, gen_sts_t = dict(gen_online), t_cycle

        # ── 2. UPDATE BEBAN (override per-channel, trip, soft-start, noise)
        #       Dengan FC23, override %MW40-51 ikut terbaca pada transaksi tulis siklus sebelumnya.
        if not use_fc23:
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
                # Soft-start menuju target profil harian / pemulihan beban saat restorasi
                diff = max_vals[i] * target_pct - current_loads[i]
                max_step = max_vals[i] * 0.02 * dt  # 2% per detik (pulih penuh dalam 50 detik)
                current_loads[i] += max(-max_step, min(max_step, diff))

                # Noise dinamis mengayun halus (rentang fluktuasi ±5%), diskalakan sqrt(dt/DT)
                if abs(diff) < max_vals[i] * 0.05:
                    load_noise_factors[i] += random.uniform(-0.002, 0.002) * (dt / DT) ** 0.5
                    load_noise_factors[i] = max(-0.05, min(0.05, load_noise_factors[i]))
                    noise = load_noise_factors[i] * max_vals[i]
                else:
                    noise = 0.0
                final_val = max(1.0, min(float(max_vals[i]), current_loads[i] + noise))

            total_load_with_noise += final_val
            load_regs.append(int(round(final_val * 10)))   # x10 -> resolusi 0.1 MW

        # Dispatch memakai beban tanpa noise agar generator tidak ikut bergetar
        total_load = sum(current_loads)

        # ── 3-4. FISIKA: dt dibagi rata menjadi sub-langkah <= DT_SUB
        n_sub = max(1, math.ceil(dt / DT_SUB - 1e-9))
        h_sub = dt / n_sub
        f_start, t_p0 = freq, time.perf_counter()
        for _ in range(n_sub):
            physics_substep(h_sub, gen_online, total_load, total_load_with_noise)
        t_phys_ms = (time.perf_counter() - t_p0) * 1000.0
        rocof = (freq - f_start) / dt   # RoCoF rata-rata selama siklus ini

        # ── 5. TULIS KE PLC
        gen_vals = [int(round(gen_output[g] * 10)) for g in GEN_ORDER]
        h_i = int(hour)
        m_i = int((hour - h_i) * 60)
        s_i = int((((hour - h_i) * 60) - m_i) * 60)
        if use_fc23:
            regs = list(fill_regs)
            regs[ADDR_LOADS:ADDR_LOADS + 12] = load_regs                 # %MW0-11
            regs[ADDR_CLOCK:ADDR_CLOCK + 2] = [h_i, m_i]                 # %MW20-21
            regs[ADDR_GEN_OUT:ADDR_GEN_OUT + 4] = gen_vals               # %MW30-33
            rw = client.readwrite_registers(read_address=ADDR_OVERRIDE, read_count=12,
                                            write_address=ADDR_LOADS, values=regs)
            if rw.isError():
                use_fc23 = False
                print(f"[IO load.py] FC23 ditolak PLC ({rw}); kembali ke tulis terpisah")
            else:
                override = list(rw.registers)
        if not use_fc23:
            client.write_registers(address=ADDR_LOADS, values=load_regs)     # %MW0-11
            client.write_registers(address=ADDR_CLOCK, values=[h_i, m_i])    # %MW20-21
            client.write_registers(address=ADDR_GEN_OUT, values=gen_vals)    # %MW30-33
        client.write_register(address=ADDR_FREQ, value=int(round(freq * 100)))  # %MW54

        # ── 6. LOG TERMINAL
        ov_str = f" | OV:{override_ch}" if override_ch else ""
        t_p0 = time.perf_counter()
        print(
            f"[{h_i:02d}:{m_i:02d}:{s_i:02d}] "
            f"LOAD:{sum(load_regs) / 10.0:.1f}MW | "
            f"GEN:{sum(gen_vals) / 10.0:.1f}MW "
            f"(PLTA:{gen_output['PLTA']:.1f} PLTS:{gen_output['PLTS']:.1f} "
            f"PLTGU:{gen_output['PLTGU']:.1f} PLTB:{gen_output['PLTB']:.1f}) | "
            f"f={freq:.3f}Hz RoCoF={rocof:+.3f}Hz/s dt={dt * 1000:.0f}ms{ov_str}"
        )
        t_print_ms = (time.perf_counter() - t_p0) * 1000.0

    except Exception as e:
        print(f"[ERROR] {e}")
        gen_sts_written = None    # setelah tersambung ulang (mis. PLC restart, memori 0) status gen ditulis lebih dulu
        try:
            client.connect()
        except Exception:
            pass

    work_ms = (time.perf_counter() - t_cycle) * 1000.0
    if work_ms > IO_SLOW_MS:
        print(f"[IO load.py] kerja siklus {work_ms:.0f} ms | modbus {_io['ms']:.0f} ms ({_io['n']} transaksi, "
              f"maks {_io['max']:.0f} ms) | print {t_print_ms:.0f} ms | fisika {t_phys_ms:.0f} ms | "
              f"lainnya {work_ms - _io['ms'] - t_print_ms - t_phys_ms:.0f} ms")
    cycle_meter.work_done()
    # Jadwal tetap: tidur hanya sisa waktu menuju DT (siklus lambat tidak menambah sleep)
    time.sleep(max(0.0, DT - (time.perf_counter() - t_cycle)))
    cycle_meter.end()
    sim_time += (dt * TIME_SPEED_FACTOR) / 3600.0   # 1.0x: 1 s nyata = 1 s simulasi (dt terukur)
