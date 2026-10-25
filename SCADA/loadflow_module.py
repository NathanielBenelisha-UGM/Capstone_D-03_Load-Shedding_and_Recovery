"""
loadflow_module.py - Optimasi load shedding & analisis aliran daya
==================================================================
- solve_milp_shedding           : MILP (PuLP/CBC) pemilihan beban yang dilepas
- run_live_loadflow             : AC load flow pandapower dari status SCADA
- verify_ac_post_shedding       : validasi tegangan & pembebanan pasca-shedding
- solve_socp_fallback           : SOCP OPF (CVXPY, DistFlow relaxation)
- solve_feeder_contingency_shedding : shedding terarah saat saluran trip

Topologi jaringan dibangun SEKALI lalu disalin (deepcopy) untuk setiap
perhitungan, karena membangun ulang jaringan pandapower jauh lebih lambat.
"""
import copy
import itertools
import time

import cvxpy as cp
import pandapower as pp
import pandas as pd
import pulp

MILP_PRIORITY_WEIGHTS = {2: 1, 3: 10, 4: 100}
# Tie-break: di antara solusi berbobot prioritas sama, pilih yang MW-nya paling kecil (kurangi over-shedding).
# eps x total MW (<= 200 MW -> 0.02) jauh di bawah selisih bobot terkecil (0.1), jadi urutan prioritas tetap.
MILP_EXCESS_EPS = 1e-4
PRIORITY_LABEL = {2: 'Non-Essential', 3: 'Essential', 4: 'Critical'}

# Nama generator di SCADA -> nama elemen di jaringan pandapower
PP_GEN_NAME = {
    'PLTA':  'Slack (GEN_1A PLTA)',
    'PLTS':  'GEN_1B PLTS',
    'PLTGU': 'GEN_2A PLTGU',
    'PLTB':  'GEN_2B PLTB',
}


# =========================================================
# MILP LOAD SHEDDING
# =========================================================
def solve_milp_shedding(deficit, live_loads, current_tripped=None, freq_hz=None,
                        must_shed_loads=None, droop_tolerance=None, keep_tripped=False, label=None):
    """keep_tripped=True: beban yang sudah padam WAJIB tetap padam (pelepasan monoton selama gangguan).
    label: judul banner log (penyebab shedding), default 'EMERGENCY UFLS TRIGGERED!'."""
    current_tripped = current_tripped or set()
    start_time = time.perf_counter()

    prob = pulp.LpProblem("LoadShedding", pulp.LpMinimize)
    shed_vars = {l['name']: pulp.LpVariable(f"shed_{l['name']}", cat='Binary')
                 for l in live_loads}

    # Droop Tolerance (Kompromi governor & spinning reserve):
    # Untuk defisit > 10 MW, toleransi 5% (min 1.0 MW, max 3.0 MW) dapat diserap
    # oleh governor droop generator online sehingga tidak memotong beban diskrit berlebih.
    if droop_tolerance is None:
        droop_tol = min(3.0, max(1.0, deficit * 0.05)) if deficit > 10.0 else 0.0
    else:
        droop_tol = float(droop_tolerance)
    effective_deficit = max(0.0, deficit - droop_tol)

    # Constraint: Total daya yang dilepas harus >= effective_deficit
    prob += pulp.lpSum([l['mw'] * shed_vars[l['name']] for l in live_loads]) >= effective_deficit

    # Objective: Minimize cost of shedding
    objective = []
    for l in live_loads:
        weight = MILP_PRIORITY_WEIGHTS.get(l.get('priority', 2), 1)
        if l['name'] in current_tripped:
            weight = weight * 0.90          # anti-osilasi: pertahankan beban yang sudah padam
            if keep_tripped:
                prob += shed_vars[l['name']] >= 1
        if must_shed_loads and l['name'] in must_shed_loads:
            weight = weight * 0.1
            prob += shed_vars[l['name']] >= 1
        objective.append((weight + MILP_EXCESS_EPS * l['mw']) * shed_vars[l['name']])
    prob += pulp.lpSum(objective)

    prob.solve(pulp.PULP_CBC_CMD(msg=0))
    exec_time = time.perf_counter() - start_time

    status = pulp.LpStatus[prob.status]
    shed_set = set()
    if status == 'Optimal':
        shed_set = {l['name'] for l in live_loads if pulp.value(shed_vars[l['name']]) > 0.5}

        # Pretty print hanya untuk UFLS yang benar-benar mengubah status beban
        if freq_hz is not None and shed_set and shed_set != current_tripped:
            total_shed = sum(l['mw'] for l in live_loads if l['name'] in shed_set)
            print("\n=============================================================")
            print(f"[MILP SOLVER] {label or 'EMERGENCY UFLS TRIGGERED!'}")
            print(f"System Frequency: {freq_hz:.2f} Hz")
            if droop_tolerance == 0:
                print(f"Target Shedding : {effective_deficit:.1f} MW (toleransi droop sudah diperhitungkan di [TARGET SHED])")
            else:
                print(f"Power Deficit   : {deficit:.1f} MW (Target Efektif: {effective_deficit:.1f} MW, Droop Tol: {droop_tol:.1f} MW)")
            print("-------------------------------------------------------------")
            print("Executing MILP Optimization (CBC Solver)...")
            print(f"Optimization Status : {status}")
            print(f"Solving Time        : {exec_time:.3f} seconds\n")
            print("[ACTION REQUIRED] Shedding the following loads:")
            for l in live_loads:
                if l['name'] in shed_set:
                    ptype = PRIORITY_LABEL.get(l.get('priority', 2), 'Unknown')
                    print(f"  - {l['name']} ({ptype}) : {l['mw']} MW")
            print(f"Total Power Shed      : {total_shed:.1f} MW")
            print("=============================================================\n")

    return shed_set, status


# =========================================================
# TOPOLOGI JARINGAN (dibangun sekali, disalin per perhitungan)
# =========================================================
def _build_network():
    net = pp.create_empty_network()

    # Buses
    b_66_1 = pp.create_bus(net, vn_kv=66., name="66kV-1 (GEN 1A, 1B)")
    b_66_2 = pp.create_bus(net, vn_kv=66., name="66kV-2 (GEN 2A, 2B)")
    b_150_1 = pp.create_bus(net, vn_kv=150., name="N1_SS1")
    b_150_2 = pp.create_bus(net, vn_kv=150., name="N2_SS2")
    b_150_3 = pp.create_bus(net, vn_kv=150., name="N3_SS3")
    b_150_4 = pp.create_bus(net, vn_kv=150., name="N4_SS4_150kV")
    b_20_1 = pp.create_bus(net, vn_kv=20., name="N5_SS4_20kV")

    # Geodata untuk plotting (menghindari error igraph)
    net.bus_geodata = pd.DataFrame(
        [[0, 10], [10, 10], [0, 5], [10, 5], [0, 0], [10, 0], [15, 0]],
        index=[b_66_1, b_66_2, b_150_1, b_150_2, b_150_3, b_150_4, b_20_1],
        columns=['x', 'y'])

    # Transformers (Kapasitas MVA diubah dari 100 ke 125 untuk generator)
    trafo = dict(vkr_percent=0.3, vk_percent=10., pfe_kw=50., i0_percent=0.1)
    pp.create_transformer_from_parameters(net, hv_bus=b_150_1, lv_bus=b_66_1, sn_mva=125., vn_hv_kv=150., vn_lv_kv=66., name="Trafo 1", **trafo)
    pp.create_transformer_from_parameters(net, hv_bus=b_150_2, lv_bus=b_66_2, sn_mva=125., vn_hv_kv=150., vn_lv_kv=66., name="Trafo 2", **trafo)
    pp.create_transformer_from_parameters(net, hv_bus=b_150_4, lv_bus=b_20_1, sn_mva=100., vn_hv_kv=150., vn_lv_kv=20., name="Trafo 3", **trafo)

    # Lines - konduktor berkas 150 kV pada ring 30 km (ACSR Grosbeak ~0.55 kA)
    # Memberikan safety margin pasca-shedding di kisaran ~85-88% (tidak mepet 100%)
    line = dict(length_km=30.0, r_ohm_per_km=0.06, x_ohm_per_km=0.30, c_nf_per_km=10.)
    pp.create_line_from_parameters(net, b_150_1, b_150_2, max_i_ka=0.50, name="Line_1", **line)
    pp.create_line_from_parameters(net, b_150_1, b_150_3, max_i_ka=0.60, name="Line_1-1", **line)
    pp.create_line_from_parameters(net, b_150_2, b_150_4, max_i_ka=0.60, name="Line_1-2", **line)
    pp.create_line_from_parameters(net, b_150_3, b_150_4, max_i_ka=0.38, name="Line_2", **line)

    # Generators - AVR diset 1.01 pu untuk profil tegangan alami & realistis
    pp.create_ext_grid(net, bus=b_66_1, vm_pu=1.01, name=PP_GEN_NAME['PLTA'])
    pp.create_sgen(net, bus=b_66_1, p_mw=25.0, name=PP_GEN_NAME['PLTS'])
    pp.create_gen(net, bus=b_66_2, p_mw=75.0, vm_pu=1.01, name=PP_GEN_NAME['PLTGU'])
    pp.create_sgen(net, bus=b_66_2, p_mw=25.0, name=PP_GEN_NAME['PLTB'])

    # Loads
    load_defs = [
        {'name': 'L101', 'bus': b_150_1, 'p_mw': 20, 'priority': 2},
        {'name': 'L201', 'bus': b_150_2, 'p_mw': 20, 'priority': 2},
        {'name': 'L301', 'bus': b_150_3, 'p_mw': 5,  'priority': 3},
        {'name': 'L302', 'bus': b_150_3, 'p_mw': 10, 'priority': 3},
        {'name': 'L303', 'bus': b_150_3, 'p_mw': 15, 'priority': 2},
        {'name': 'L304', 'bus': b_150_3, 'p_mw': 20, 'priority': 2},
        {'name': 'L305', 'bus': b_150_3, 'p_mw': 30, 'priority': 2},
        {'name': 'L401', 'bus': b_150_4, 'p_mw': 30, 'priority': 2},
        {'name': 'L402', 'bus': b_20_1,  'p_mw': 5,  'priority': 3},
        {'name': 'L403', 'bus': b_20_1,  'p_mw': 10, 'priority': 4},
        {'name': 'L404', 'bus': b_20_1,  'p_mw': 15, 'priority': 4},
        {'name': 'L405', 'bus': b_20_1,  'p_mw': 20, 'priority': 4},
    ]
    for ld in load_defs:
        pp.create_load(net, bus=ld['bus'], p_mw=ld['p_mw'], q_mvar=ld['p_mw'] * 0.25, name=ld['name'])

    return net, load_defs


_BASE_NET, _BASE_LOAD_DEFS = _build_network()
LOAD_BUS = {ld['name']: ld['bus'] for ld in _BASE_LOAD_DEFS}
LINE_BUS_MAP = {row['name']: [row['from_bus'], row['to_bus']] for _, row in _BASE_NET.line.iterrows()}
TRAFO_BUS_MAP = {row['name']: [row['hv_bus'], row['lv_bus']] for _, row in _BASE_NET.trafo.iterrows()}


def create_network():
    """Salinan baru jaringan dasar (tidak membangun ulang dari nol)."""
    return copy.deepcopy(_BASE_NET), [dict(ld) for ld in _BASE_LOAD_DEFS]


def loads_on_buses(bus_indices):
    """Nama beban yang terhubung ke bus-bus tertentu (urutan seperti load_defs)."""
    return [ld['name'] for ld in _BASE_LOAD_DEFS if ld['bus'] in bus_indices]


def _first_index(table, name):
    idx = table[table.name == name].index
    return idx[0] if len(idx) > 0 else None


def _apply_line_statuses(net, line_statuses):
    for l_name, status in (line_statuses or {}).items():
        i = _first_index(net.line, l_name)
        if i is not None:
            net.line.loc[i, 'in_service'] = (status == 'ONLINE')


def _apply_gen_statuses(net, gen_statuses, min_p_mw=None):
    """Set in_service & p_mw generator dari status SCADA.
    min_p_mw: batas bawah p_mw (None = pakai nilai sensor apa adanya)."""
    for g in gen_statuses:
        pp_name = PP_GEN_NAME.get(g['name'])
        if not pp_name:
            continue
        is_on = g['status'] == 'ONLINE'
        p_mw = g['mw'] if (min_p_mw is None or g['mw'] > 0) else min_p_mw

        i = _first_index(net.ext_grid, pp_name)
        if i is not None:
            net.ext_grid.loc[i, 'in_service'] = is_on
        for table in (net.gen, net.sgen):
            i = _first_index(table, pp_name)
            if i is not None:
                table.loc[i, 'in_service'] = is_on
                table.loc[i, 'p_mw'] = p_mw


# =========================================================
# AC LOAD FLOW
# =========================================================
def get_results_dict(net):
    v_profile = net.res_bus[['vm_pu', 'va_degree']].round(3).fillna(0)
    v_profile.index = net.bus.name

    t_load = net.res_trafo[['loading_percent']].round(2).fillna(0)
    t_load.index = net.trafo.name

    l_load = net.res_line[['loading_percent']].round(2).fillna(0)
    l_load.index = net.line.name

    def _max_of(df):
        if not df.empty and not pd.isna(df['loading_percent'].max()):
            return float(df['loading_percent'].max()), str(df['loading_percent'].idxmax())
        return 0.0, "-"

    max_line_val, max_line_name = _max_of(l_load)
    max_trafo_val, max_trafo_name = _max_of(t_load)

    slack_mw = 0
    slack_name = "PLTA"
    slack_capacity = 75.0
    if not net.res_ext_grid.empty:
        active_ext = net.ext_grid[net.ext_grid.in_service == True]  # noqa: E712
        if not active_ext.empty:
            slack_name = active_ext.loc[active_ext.index[0], 'name']
            for gen_key, cap in (('PLTA', 75.0), ('PLTGU', 75.0), ('PLTS', 25.0), ('PLTB', 25.0)):
                if gen_key in slack_name:
                    slack_capacity = cap
                    break
        slack_mw = net.res_ext_grid.p_mw.sum()

    active_v = v_profile[v_profile['vm_pu'] > 0]
    min_v = active_v['vm_pu'].min() if not active_v.empty else 0.0
    max_v = active_v['vm_pu'].max() if not active_v.empty else 0.0

    return {
        'buses': v_profile.reset_index().to_dict(orient='records'),
        'trafos': t_load.reset_index().to_dict(orient='records'),
        'lines': l_load.reset_index().to_dict(orient='records'),
        'slack_mw': float(slack_mw),
        'slack_name': slack_name,
        'slack_capacity': float(slack_capacity),
        'max_loading': float(max(max_line_val, max_trafo_val)),
        'max_trafo_loading': float(max_trafo_val),
        'max_trafo_name': str(max_trafo_name),
        'max_line_loading': float(max_line_val),
        'max_line_name': str(max_line_name),
        'min_v': float(min_v),
        'max_v': float(max_v),
    }


def run_live_loadflow(gen_statuses, live_loads, tripped_loads, line_statuses=None):
    """
    Menjalankan pandapower berdasarkan status aktual SCADA.
    gen_statuses : list dict {'name': 'PLTA', 'status': 'ONLINE', 'mw': ...}
    live_loads   : list dict {'name': 'L101', 'mw': ..., 'actual_mw': ...}
    tripped_loads: set nama beban yang mati
    line_statuses: dict {'Line_1': 'ONLINE', ...}
    """
    net, _ = create_network()
    _apply_gen_statuses(net, gen_statuses, min_p_mw=0.1)

    # Trafo sengaja tidak dimatikan walau bus generator mati total: pandapower akan
    # menganggap bus 66 kV sebagai island tanpa slack dan gagal konvergen.

    for l in live_loads:
        i = _first_index(net.load, l['name'])
        if i is not None:
            net.load.loc[i, 'in_service'] = l['name'] not in tripped_loads
            # actual_mw dengan daya reaktif realistis (cos phi ~0.97 -> Q/P = 0.25)
            p = l.get('actual_mw', l['mw'])
            net.load.loc[i, 'p_mw'] = p if p > 0 else 0.1
            net.load.loc[i, 'q_mvar'] = (p * 0.25) if p > 0 else 0.02

    _apply_line_statuses(net, line_statuses)

    # Pastikan ada slack bus aktif (mencegah error 'No reference bus')
    if net.ext_grid[net.ext_grid.in_service == True].empty:  # noqa: E712
        replaced = False
        for table in (net.gen, net.sgen):
            active = table[table.in_service == True]  # noqa: E712
            if not active.empty:
                i = active.index[0]
                table.loc[i, 'in_service'] = False
                pp.create_ext_grid(net, bus=table.loc[i, 'bus'], vm_pu=1.01,
                                   name=f"Slack ({table.loc[i, 'name']})")
                replaced = True
                break
        if not replaced:
            return {'status': 'error', 'message': 'TOTAL BLACKOUT - Tidak ada generator aktif'}

    try:
        pp.runpp(net, enforce_q_lims=False, max_iteration=50)
        res = get_results_dict(net)
        res['status'] = 'success'
    except pp.LoadflowNotConverged:
        res = {'status': 'error', 'message': 'Loadflow Not Converged (Blackout / Extreme Deficit)'}
    return res


def verify_ac_post_shedding(gen_statuses, live_loads, proposed_shed_set, line_statuses=None):
    """
    AC load flow untuk memverifikasi tegangan & pembebanan setelah shedding diusulkan.
    Returns: (is_valid, list_of_violating_bus)
    """
    res = run_live_loadflow(gen_statuses, live_loads, proposed_shed_set, line_statuses)
    if res.get('status') == 'error':
        return False, ["ALL_BUSES"]  # gagal konvergen

    # Grid Code: Normal 0.95-1.05 pu, Kontingensi (N-1) 0.90-1.05 pu
    is_contingency = bool(line_statuses and any(s != 'ONLINE' for s in line_statuses.values()))
    v_min_limit = 0.90 if is_contingency else 0.95
    v_max_limit = 1.05
    violating_buses = []

    for i, b in enumerate(res['buses']):
        val = b.get('vm_pu')
        if val is None or pd.isna(val) or val < v_min_limit or val > v_max_limit:
            bus_idx = b.get('index', b.get('bus_id', b.get('level_0', i)))
            if bus_idx not in violating_buses:
                violating_buses.append(bus_idx)

    # N-1 security margin: saat gangguan saluran, pembebanan saluran dibatasi <= 85%
    line_limit = 85.0 if is_contingency else 100.0
    trafo_limit = 100.0
    for items, bus_map, limit in ((res.get('lines', []), LINE_BUS_MAP, line_limit),
                                  (res.get('trafos', []), TRAFO_BUS_MAP, trafo_limit)):
        for item in items:
            if item.get('loading_percent', 0) > limit:
                for b_idx in bus_map.get(item.get('name'), []):
                    if b_idx not in violating_buses:
                        violating_buses.append(b_idx)

    return len(violating_buses) == 0, violating_buses


# =========================================================
# SOCP OPF FALLBACK
# =========================================================
def solve_socp_fallback(gen_statuses, live_loads, current_tripped=None, line_statuses=None):
    """
    Matematika murni SOCP (Second-Order Cone Programming),
    Branch Flow Model (DistFlow relaxation) via CVXPY.
    """
    net, _ = create_network()
    _apply_line_statuses(net, line_statuses)
    _apply_gen_statuses(net, gen_statuses)

    buses = net.bus.index.tolist()
    n_bus = len(buses)
    bus_map = {b: i for i, b in enumerate(buses)}

    branches = []
    for _, row in net.line.iterrows():
        if not row.in_service:
            continue
        zb = (net.bus.vn_kv.loc[row.from_bus] ** 2) / 100.0
        branches.append({'from': bus_map[row.from_bus], 'to': bus_map[row.to_bus],
                         'r': (row.length_km * row.r_ohm_per_km) / zb,
                         'x': (row.length_km * row.x_ohm_per_km) / zb})
    for _, row in net.trafo.iterrows():
        # impedansi trafo disederhanakan
        branches.append({'from': bus_map[row.hv_bus], 'to': bus_map[row.lv_bus], 'r': 0.01, 'x': 0.05})
    n_branch = len(branches)

    v_sq = cp.Variable(n_bus)
    P_ij = cp.Variable(n_branch)
    Q_ij = cp.Variable(n_branch)
    l_ij = cp.Variable(n_branch)
    P_shed = cp.Variable(len(live_loads))

    # Slack bus untuk keseimbangan OPF
    slack_idx = None
    if net.ext_grid[net.ext_grid.in_service == True].empty:  # noqa: E712
        for table in (net.gen, net.sgen):
            active = table[table.in_service == True]  # noqa: E712
            if not active.empty:
                slack_idx = bus_map[active.bus.values[0]]
                break
    if slack_idx is None:
        slack_idx = bus_map[net.ext_grid.bus.values[0]]  # fallback default

    constraints = [v_sq >= 0.9025, v_sq <= 1.1025]  # 0.95^2 s.d. 1.05^2
    constraints.append(v_sq[slack_idx] == 1.0)

    for k, br in enumerate(branches):
        i, j = br['from'], br['to']
        # Cone: P^2 + Q^2 <= l_ij * v_i
        constraints.append(cp.quad_over_lin(cp.vstack([P_ij[k], Q_ij[k]]), v_sq[i]) <= l_ij[k])
        # Voltage drop
        constraints.append(v_sq[j] == v_sq[i] - 2 * (br['r'] * P_ij[k] + br['x'] * Q_ij[k])
                           + (br['r'] ** 2 + br['x'] ** 2) * l_ij[k])

    load_bus = {}
    for k_load, l_dict in enumerate(live_loads):
        row = net.load[net.load.name == l_dict['name']]
        if not row.empty:
            load_bus[k_load] = bus_map[row.bus.values[0]]

    for i in range(n_bus):
        P_g, Q_g = 0, 0
        if i == slack_idx:
            p_var, q_var = cp.Variable(), cp.Variable()
            constraints.extend([p_var >= 0, p_var <= 0.75, q_var >= -0.5, q_var <= 0.5])
            P_g += p_var
            Q_g += q_var
        for _, row in net.gen.iterrows():
            if bus_map[row.bus] == i and row.in_service and i != slack_idx:
                p_var, q_var = cp.Variable(), cp.Variable()
                # generator boleh mengurangi output bila island kelebihan pembangkitan
                constraints.extend([p_var >= 0, p_var <= row.p_mw / 100.0, q_var >= -0.5, q_var <= 0.5])
                P_g += p_var
                Q_g += q_var
        for _, row in net.sgen.iterrows():
            if bus_map[row.bus] == i and row.in_service and i != slack_idx:
                p_var = cp.Variable()
                constraints.extend([p_var >= 0, p_var <= row.p_mw / 100.0])
                P_g += p_var

        P_l, Q_l = 0, 0
        for k_load, l_dict in enumerate(live_loads):
            if load_bus.get(k_load) == i:
                load_mw_pu = l_dict['mw'] / 100.0
                constraints.extend([P_shed[k_load] >= 0, P_shed[k_load] <= load_mw_pu])
                P_l += (load_mw_pu - P_shed[k_load])
                Q_l += (load_mw_pu - P_shed[k_load]) * 0.2

        out_P = sum(P_ij[k] for k, br in enumerate(branches) if br['from'] == i)
        out_Q = sum(Q_ij[k] for k, br in enumerate(branches) if br['from'] == i)
        in_P = sum(P_ij[k] - br['r'] * l_ij[k] for k, br in enumerate(branches) if br['to'] == i)
        in_Q = sum(Q_ij[k] - br['x'] * l_ij[k] for k, br in enumerate(branches) if br['to'] == i)
        constraints.extend([P_g - P_l == out_P - in_P, Q_g - Q_l == out_Q - in_Q])

    obj = sum(MILP_PRIORITY_WEIGHTS.get(l.get('priority', 2), 1) * P_shed[k]
              for k, l in enumerate(live_loads))
    prob = cp.Problem(cp.Minimize(obj), constraints)
    try:
        prob.solve(verbose=False)
    except Exception as e:
        print("SOCP SOLVE EXCEPTION:", e)

    shed_set = set()
    if prob.status in ["optimal", "optimal_inaccurate"]:
        for k, l_dict in enumerate(live_loads):
            val = P_shed[k].value
            if val is not None and (val * 100.0) > (l_dict['mw'] * 0.5):  # kontinyu -> diskrit
                shed_set.add(l_dict['name'])
        shed_set |= set(current_tripped or ())   # monoton: beban yang sudah padam tidak dinyalakan
    return shed_set, prob.status


# =========================================================
# FEEDER CONTINGENCY (SALURAN TRIP)
# =========================================================
def solve_feeder_contingency_shedding(gen_statuses, live_loads, current_tripped, line_statuses, viol_buses):
    """
    Targeted locational load shedding untuk feeder contingency:
    1. Utamakan beban di gardu yang kehilangan suplai transmisi langsung.
    2. Uji beban Prioritas 2 terbesar lebih dulu, satu per satu.
    3. Jika 1 beban cukup memulihkan kondisi aman, hanya itu yang dilepas.
    4. Jika belum, uji pasangan 2 beban (utamakan beban lokal).
    5. Jika Prioritas 2 habis, lanjut ke Prioritas 3.
    6. Fallback ke SOCP OPF jika tidak ditemukan kombinasi.
    """
    candidates = [l for l in live_loads if l['name'] not in current_tripped]

    local_prefix = ''
    if line_statuses:
        if line_statuses.get('Line_1-1') == 'OFFLINE':
            local_prefix = 'L3'  # Gardu Induk N3
        elif line_statuses.get('Line_1-2') == 'OFFLINE':
            local_prefix = 'L4'  # Gardu Induk N4

    def is_local(l):
        return bool(local_prefix) and l['name'].startswith(local_prefix)

    def mw(l):
        return l.get('actual_mw', l.get('mw', 0))

    for target_prio in [2, 3]:
        prio_cands = [l for l in candidates if l.get('priority', 2) == target_prio]
        prio_cands.sort(key=lambda x: (1 if is_local(x) else 0, mw(x)), reverse=True)

        for cand in prio_cands:
            test_set = current_tripped | {cand['name']}
            if verify_ac_post_shedding(gen_statuses, live_loads, test_set, line_statuses)[0]:
                return test_set, f"Targeted Single ({cand['name']})"

        pairs = sorted(itertools.combinations(prio_cands, 2),
                       key=lambda p: (-sum(1 for x in p if is_local(x)), sum(mw(x) for x in p)))
        for a, b in pairs:
            test_set = current_tripped | {a['name'], b['name']}
            if verify_ac_post_shedding(gen_statuses, live_loads, test_set, line_statuses)[0]:
                return test_set, f"Targeted Pair ({a['name']}+{b['name']})"

    shed_socp, _ = solve_socp_fallback(gen_statuses, live_loads, current_tripped, line_statuses)
    return shed_socp, "SOCP Fallback"
