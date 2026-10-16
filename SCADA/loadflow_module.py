import pandapower as pp
import pulp
import pandas as pd
import os
import cvxpy as cp
import numpy as np
import itertools

def solve_milp_shedding(deficit, live_loads, current_tripped=set(), must_shed_loads=None):
    prob = pulp.LpProblem("LoadShedding", pulp.LpMinimize)
    shed_vars = {}
    for l in live_loads:
        shed_vars[l['name']] = pulp.LpVariable(f"shed_{l['name']}", cat='Binary')
        
    prob += pulp.lpSum([l['mw'] * shed_vars[l['name']] for l in live_loads]) >= deficit
    
    MILP_PRIORITY_WEIGHTS = {2: 1, 3: 10, 4: 100}
    objective = []
    for l in live_loads:
        weight = MILP_PRIORITY_WEIGHTS.get(l.get('priority', 2), 1)
        if l['name'] in current_tripped:
            weight = weight * 0.90
            
        # Penalti tambahan untuk Augmentasi-2
        if must_shed_loads and l['name'] in must_shed_loads:
            weight = weight * 0.1 # Prioritaskan diputus (biaya sangat murah)
            prob += shed_vars[l['name']] >= 1 # Memaksa diputus jika memungkinkan
            
        objective.append(weight * shed_vars[l['name']])
        
    prob += pulp.lpSum(objective)
    solver = pulp.PULP_CBC_CMD(msg=0)
    prob.solve(solver)
    
    status = pulp.LpStatus[prob.status]
    shed_set = set()
    if status == 'Optimal':
        for load in live_loads:
            if pulp.value(shed_vars[load['name']]) > 0.5:
                shed_set.add(load['name'])
    return shed_set, status

def create_network():
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
    net.bus_geodata = pd.DataFrame(columns=['x', 'y'])
    net.bus_geodata.loc[b_66_1] = [0, 10]
    net.bus_geodata.loc[b_66_2] = [10, 10]
    net.bus_geodata.loc[b_150_1] = [0, 5]
    net.bus_geodata.loc[b_150_2] = [10, 5]
    net.bus_geodata.loc[b_150_3] = [0, 0]
    net.bus_geodata.loc[b_150_4] = [10, 0]
    net.bus_geodata.loc[b_20_1] = [15, 0]

    # Transformers (Kapasitas MVA diubah dari 100 ke 125 untuk generator)
    pp.create_transformer_from_parameters(net, hv_bus=b_150_1, lv_bus=b_66_1, sn_mva=125., vn_hv_kv=150., vn_lv_kv=66., vkr_percent=0.3, vk_percent=10., pfe_kw=50., i0_percent=0.1, name="Trafo 1")
    pp.create_transformer_from_parameters(net, hv_bus=b_150_2, lv_bus=b_66_2, sn_mva=125., vn_hv_kv=150., vn_lv_kv=66., vkr_percent=0.3, vk_percent=10., pfe_kw=50., i0_percent=0.1, name="Trafo 2")
    pp.create_transformer_from_parameters(net, hv_bus=b_150_4, lv_bus=b_20_1, sn_mva=100., vn_hv_kv=150., vn_lv_kv=20., vkr_percent=0.3, vk_percent=10., pfe_kw=50., i0_percent=0.1, name="Trafo 3")

    # Lines — Spesifikasi konduktor berkas 150 kV pada ring 30 km (ACSR Grosbeak ~0.55 kA)
    # Memberikan safety margin pasca-shedding di kisaran ~85-88% (tidak mepet 100%)
    line_length = 30.0
    pp.create_line_from_parameters(net, b_150_1, b_150_2, length_km=line_length, r_ohm_per_km=0.06, x_ohm_per_km=0.30, c_nf_per_km=10., max_i_ka=0.50, name="Line_1")
    pp.create_line_from_parameters(net, b_150_1, b_150_3, length_km=line_length, r_ohm_per_km=0.06, x_ohm_per_km=0.30, c_nf_per_km=10., max_i_ka=0.55, name="Line_1-1")
    pp.create_line_from_parameters(net, b_150_2, b_150_4, length_km=line_length, r_ohm_per_km=0.06, x_ohm_per_km=0.30, c_nf_per_km=10., max_i_ka=0.55, name="Line_1-2")
    pp.create_line_from_parameters(net, b_150_3, b_150_4, length_km=line_length, r_ohm_per_km=0.06, x_ohm_per_km=0.30, c_nf_per_km=10., max_i_ka=0.38, name="Line_2")

    # Generators — AVR diset 1.01 pu untuk profil tegangan alami & realistis
    pp.create_ext_grid(net, bus=b_66_1, vm_pu=1.01, name="Slack (GEN_1A PLTA)")
    pp.create_sgen(net, bus=b_66_1, p_mw=25.0, name="GEN_1B PLTS")
    
    pp.create_gen(net, bus=b_66_2, p_mw=75.0, vm_pu=1.01, name="GEN_2A PLTGU")
    pp.create_sgen(net, bus=b_66_2, p_mw=25.0, name="GEN_2B PLTB")
    
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

def get_results_dict(net):
    v_profile = net.res_bus[['vm_pu', 'va_degree']].round(3)
    v_profile = v_profile.fillna(0)
    v_profile.index = net.bus.name
    
    t_load = net.res_trafo[['loading_percent']].round(2)
    t_load = t_load.fillna(0)
    t_load.index = net.trafo.name
    
    l_load = net.res_line[['loading_percent']].round(2)
    l_load = l_load.fillna(0)
    l_load.index = net.line.name
    
    # Check max overload & identity
    if not l_load.empty and not pd.isna(l_load['loading_percent'].max()):
        max_line_val = float(l_load['loading_percent'].max())
        max_line_name = str(l_load['loading_percent'].idxmax())
    else:
        max_line_val = 0.0
        max_line_name = "-"

    if not t_load.empty and not pd.isna(t_load['loading_percent'].max()):
        max_trafo_val = float(t_load['loading_percent'].max())
        max_trafo_name = str(t_load['loading_percent'].idxmax())
    else:
        max_trafo_val = 0.0
        max_trafo_name = "-"

    max_loading = max(max_line_val, max_trafo_val)
    
    slack_mw = 0
    slack_name = "PLTA"
    slack_capacity = 75.0
    
    if not net.res_ext_grid.empty:
        active_ext = net.ext_grid[net.ext_grid.in_service == True]
        if not active_ext.empty:
            idx = active_ext.index[0]
            slack_name = active_ext.loc[idx, 'name']
            if 'PLTA' in slack_name: slack_capacity = 75.0
            elif 'PLTGU' in slack_name: slack_capacity = 75.0
            elif 'PLTS' in slack_name: slack_capacity = 25.0
            elif 'PLTB' in slack_name: slack_capacity = 25.0
            
        slack_mw = net.res_ext_grid.p_mw.sum()
        print("DEBUG res_ext_grid:\n", net.res_ext_grid)
        print("DEBUG slack_mw:", slack_mw)
    
    active_v = v_profile[v_profile['vm_pu'] > 0]
    if not active_v.empty:
        min_v = active_v['vm_pu'].min()
        max_v = active_v['vm_pu'].max()
    else:
        min_v = 0.0
        max_v = 0.0
    
    return {
        'buses': v_profile.reset_index().to_dict(orient='records'),
        'trafos': t_load.reset_index().to_dict(orient='records'),
        'lines': l_load.reset_index().to_dict(orient='records'),
        'slack_mw': float(slack_mw),
        'slack_name': slack_name,
        'slack_capacity': float(slack_capacity),
        'max_loading': float(max_loading),
        'max_trafo_loading': float(max_trafo_val),
        'max_trafo_name': str(max_trafo_name),
        'max_line_loading': float(max_line_val),
        'max_line_name': str(max_line_name),
        'min_v': float(min_v),
        'max_v': float(max_v)
    }

def run_live_loadflow(gen_statuses, live_loads, tripped_loads, line_statuses=None):
    """
    Menjalankan pandapower berdasarkan status aktual SCADA
    gen_statuses: list of dict {'name': 'PLTA', 'status': 'ONLINE', 'mw': ...}
    live_loads: list of dict {'name': 'L101', 'mw': ...}
    tripped_loads: set/list of string load names yang mati
    line_statuses: dict {'Line_1': 'ONLINE', ...}
    """
    net, load_defs = create_network()
    
    # Update Generators
    for g in gen_statuses:
        # Mapping nama SCADA ke nama Pandapower
        pp_name = None
        if g['name'] == 'PLTA': pp_name = 'Slack (GEN_1A PLTA)'
        elif g['name'] == 'PLTS': pp_name = 'GEN_1B PLTS'
        elif g['name'] == 'PLTGU': pp_name = 'GEN_2A PLTGU'
        elif g['name'] == 'PLTB': pp_name = 'GEN_2B PLTB'
        
        if not pp_name: continue
            
        is_on = g['status'] == 'ONLINE'
        
        # Slack bus (ext_grid) atau generator biasa (gen/sgen)
        idx_ext = net.ext_grid[net.ext_grid.name == pp_name].index
        if len(idx_ext) > 0:
            net.ext_grid.loc[idx_ext[0], 'in_service'] = is_on
        
        idx_gen = net.gen[net.gen.name == pp_name].index
        if len(idx_gen) > 0:
            net.gen.loc[idx_gen[0], 'in_service'] = is_on
            net.gen.loc[idx_gen[0], 'p_mw'] = g['mw'] if g['mw'] > 0 else 0.1
            
        idx_sgen = net.sgen[net.sgen.name == pp_name].index
        if len(idx_sgen) > 0:
            net.sgen.loc[idx_sgen[0], 'in_service'] = is_on
            net.sgen.loc[idx_sgen[0], 'p_mw'] = g['mw'] if g['mw'] > 0 else 0.1

    # Breaker Intertrip Logic dihapus: 
    # Jangan matikan Trafo meskipun bus generator mati total, karena pandapower 
    # akan menganggap bus 66kV menjadi 'island' terisolasi tanpa slack bus dan CRASH.
    # Membiarkan trafo menyala secara matematis aman (hanya akan menarik no-load loss).

    # Update Loads
    for l in live_loads:
        pp_name = l['name']
        idx_load = net.load[net.load.name == pp_name].index
        if len(idx_load) > 0:
            is_on = pp_name not in tripped_loads
            net.load.loc[idx_load[0], 'in_service'] = is_on
            # Gunakan actual_mw dan sertakan daya reaktif realistis (cosphi ~0.97 -> Q/P = 0.25)
            current_p = l.get('actual_mw', l['mw'])
            net.load.loc[idx_load[0], 'p_mw'] = current_p if current_p > 0 else 0.1
            net.load.loc[idx_load[0], 'q_mvar'] = (current_p * 0.25) if current_p > 0 else 0.02

    # Update Transmission Lines (Feeder Contingencies)
    if line_statuses:
        for l_name, status in line_statuses.items():
            idx_line = net.line[net.line.name == l_name].index
            if len(idx_line) > 0:
                net.line.loc[idx_line[0], 'in_service'] = (status == 'ONLINE')

    # Ensure at least one slack bus is active (prevent 'No reference bus' error)
    if net.ext_grid[net.ext_grid.in_service == True].empty:
        active_gens = net.gen[net.gen.in_service == True]
        if not active_gens.empty:
            gen_idx = active_gens.index[0]
            bus_idx = net.gen.loc[gen_idx, 'bus']
            gen_name = net.gen.loc[gen_idx, 'name']
            net.gen.loc[gen_idx, 'in_service'] = False # Disable gen, replace with ext_grid
            pp.create_ext_grid(net, bus=bus_idx, vm_pu=1.01, name=f"Slack ({gen_name})")
        else:
            active_sgens = net.sgen[net.sgen.in_service == True]
            if not active_sgens.empty:
                sgen_idx = active_sgens.index[0]
                bus_idx = net.sgen.loc[sgen_idx, 'bus']
                sgen_name = net.sgen.loc[sgen_idx, 'name']
                net.sgen.loc[sgen_idx, 'in_service'] = False
                pp.create_ext_grid(net, bus=bus_idx, vm_pu=1.01, name=f"Slack ({sgen_name})")
            else:
                # TOTAL BLACKOUT (Semua generator mati)
                return {'status': 'error', 'message': 'TOTAL BLACKOUT - Tidak ada generator aktif'}

    # Run Power Flow
    try:
        pp.runpp(net, enforce_q_lims=False, max_iteration=50)
        res = get_results_dict(net)
        res['status'] = 'success'
        
        # Gambar plot diabaikan (bypass) karena SVG diload dari frontend untuk render lebih instan
        # save_plot(net, "plot_live.png")
        
    except pp.LoadflowNotConverged:
        res = {'status': 'error', 'message': 'Loadflow Not Converged (Blackout / Extreme Deficit)'}
        
    return res
def verify_ac_post_shedding(gen_statuses, live_loads, proposed_shed_set, line_statuses=None):
    """
    Menjalankan simulasi AC Load Flow untuk memverifikasi tegangan dan loading
    setelah MILP/targeted shedding mengusulkan pemutusan beban.
    Returns: (is_valid, list_of_violating_bus_names)
    """
    # Run AC Load Flow simulasi dengan `proposed_shed_set` dan `line_statuses`
    res = run_live_loadflow(gen_statuses, live_loads, proposed_shed_set, line_statuses)
    if res.get('status') == 'error':
        return False, ["ALL_BUSES"] # Gagal konvergen
        
    import pandas as pd
    buses = res['buses']
    violating_buses = []
    
    # Standar Grid Code: Normal 0.95 - 1.05 pu, Kontingensi Darurat (N-1) 0.90 - 1.05 pu
    is_contingency = bool(line_statuses and any(s != 'ONLINE' for s in line_statuses.values()))
    v_min_limit = 0.90 if is_contingency else 0.95
    v_max_limit = 1.05

    for i, b in enumerate(buses):
        val = b.get('vm_pu')
        if pd.isna(val) or val is None or val < v_min_limit or val > v_max_limit:
            bus_idx = b.get('index', b.get('bus_id', b.get('level_0', i)))
            if bus_idx not in violating_buses:
                violating_buses.append(bus_idx)

    # Cek loading saluran transmisi & trafo — mapping bus dinamis dari topologi
    # Standar N-1 Security Margin: selama gangguan saluran transmisi, target pasca-shedding
    # dibatasi <= 85% agar kawat beroperasi dingin dan menyisakan cadangan termal.
    line_loading_limit = 85.0 if is_contingency else 100.0
    trafo_loading_limit = 100.0

    net, _ = create_network()
    # Buat dict nama line -> [from_bus, to_bus]
    line_bus_map = {row['name']: [row['from_bus'], row['to_bus']]
                   for _, row in net.line.iterrows()}
    # Buat dict nama trafo -> [hv_bus, lv_bus]
    trafo_bus_map = {row['name']: [row['hv_bus'], row['lv_bus']]
                    for _, row in net.trafo.iterrows()}

    for line in res.get('lines', []):
        if line.get('loading_percent', 0) > line_loading_limit:
            for b_idx in line_bus_map.get(line.get('name'), []):
                if b_idx not in violating_buses:
                    violating_buses.append(b_idx)

    for trafo in res.get('trafos', []):
        if trafo.get('loading_percent', 0) > trafo_loading_limit:
            for b_idx in trafo_bus_map.get(trafo.get('name'), []):
                if b_idx not in violating_buses:
                    violating_buses.append(b_idx)
            
    return len(violating_buses) == 0, violating_buses

def solve_socp_fallback(gen_statuses, live_loads, current_tripped=set(), line_statuses=None):
    """
    Matematika murni SOCP (Second-Order Cone Programming)
    Menggunakan Branch Flow Model (DistFlow Relaxation) via CVXPY.
    """
    # Create net with line contingencies applied
    net, _ = create_network()
    if line_statuses:
        for l_name, status in line_statuses.items():
            idx_line = net.line[net.line.name == l_name].index
            if len(idx_line) > 0:
                net.line.loc[idx_line[0], 'in_service'] = (status == 'ONLINE')
    
    # 1. Update In-Service Status di Network
    for g in gen_statuses:
        pp_name = None
        if g['name'] == 'PLTA': pp_name = 'Slack (GEN_1A PLTA)'
        elif g['name'] == 'PLTS': pp_name = 'GEN_1B PLTS'
        elif g['name'] == 'PLTGU': pp_name = 'GEN_2A PLTGU'
        elif g['name'] == 'PLTB': pp_name = 'GEN_2B PLTB'
        if not pp_name: continue
        is_on = g['status'] == 'ONLINE'
        
        idx_ext = net.ext_grid[net.ext_grid.name == pp_name].index
        if len(idx_ext) > 0: net.ext_grid.loc[idx_ext[0], 'in_service'] = is_on
        idx_gen = net.gen[net.gen.name == pp_name].index
        if len(idx_gen) > 0: 
            net.gen.loc[idx_gen[0], 'in_service'] = is_on
            net.gen.loc[idx_gen[0], 'p_mw'] = g['mw']
        idx_sgen = net.sgen[net.sgen.name == pp_name].index
        if len(idx_sgen) > 0: 
            net.sgen.loc[idx_sgen[0], 'in_service'] = is_on
            net.sgen.loc[idx_sgen[0], 'p_mw'] = g['mw']
            
    # 2. Extract Data untuk SOCP
    buses = net.bus.index.tolist()
    n_bus = len(buses)
    bus_map = {b: i for i, b in enumerate(buses)}
    
    branches = []
    for _, row in net.line.iterrows():
        if not row.in_service: continue
        zb = (net.bus.vn_kv.loc[row.from_bus] ** 2) / 100.0
        branches.append({'from': bus_map[row.from_bus], 'to': bus_map[row.to_bus], 
                         'r': (row.length_km * row.r_ohm_per_km)/zb, 'x': (row.length_km * row.x_ohm_per_km)/zb})
    for _, row in net.trafo.iterrows():
        # simplified trafo impedance
        branches.append({'from': bus_map[row.hv_bus], 'to': bus_map[row.lv_bus], 'r': 0.01, 'x': 0.05})
        
    n_branch = len(branches)
    
    # 3. Define Variables
    v_sq = cp.Variable(n_bus)
    P_ij = cp.Variable(n_branch)
    Q_ij = cp.Variable(n_branch)
    l_ij = cp.Variable(n_branch)
    P_shed = cp.Variable(len(live_loads))
    
    # Ensure at least one slack bus is active for OPF balance
    slack_idx = None
    if net.ext_grid[net.ext_grid.in_service == True].empty:
        active_gens = net.gen[net.gen.in_service == True]
        if not active_gens.empty:
            slack_idx = bus_map[active_gens.bus.values[0]]
        else:
            active_sgens = net.sgen[net.sgen.in_service == True]
            if not active_sgens.empty:
                slack_idx = bus_map[active_sgens.bus.values[0]]
    if slack_idx is None:
        slack_idx = bus_map[net.ext_grid.bus.values[0]] # fallback default

    # 4. Constraints
    constraints = [v_sq >= 0.9025, v_sq <= 1.1025] # 0.95^2 to 1.05^2
    constraints.append(v_sq[slack_idx] == 1.0)
    
    for k, br in enumerate(branches):
        i, j = br['from'], br['to']
        # Cone constraint: P^2 + Q^2 <= l_ij * v_i
        constraints.append(cp.quad_over_lin(cp.vstack([P_ij[k], Q_ij[k]]), v_sq[i]) <= l_ij[k])
        # Voltage drop
        constraints.append(v_sq[j] == v_sq[i] - 2*(br['r']*P_ij[k] + br['x']*Q_ij[k]) + (br['r']**2 + br['x']**2)*l_ij[k])
        
    for i in range(n_bus):
        P_g = 0
        Q_g = 0
        
        if i == slack_idx:
            p_var = cp.Variable()
            q_var = cp.Variable()
            constraints.extend([p_var >= 0, p_var <= 0.75, q_var >= -0.5, q_var <= 0.5])
            P_g += p_var
            Q_g += q_var
            
        for _, row in net.gen.iterrows():
            if bus_map[row.bus] == i and row.in_service and i != slack_idx: 
                p_var = cp.Variable()
                q_var = cp.Variable()
                # Allow generator to curtail output if island is over-generated
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
            load_row = net.load[net.load.name == l_dict['name']]
            if not load_row.empty and bus_map[load_row.bus.values[0]] == i:
                load_mw_pu = l_dict['mw'] / 100.0
                constraints.extend([P_shed[k_load] >= 0, P_shed[k_load] <= load_mw_pu])
                P_l += (load_mw_pu - P_shed[k_load])
                Q_l += (load_mw_pu - P_shed[k_load]) * 0.2
                
        out_P = sum(P_ij[k] for k, br in enumerate(branches) if br['from'] == i)
        out_Q = sum(Q_ij[k] for k, br in enumerate(branches) if br['from'] == i)
        in_P = sum(P_ij[k] - br['r']*l_ij[k] for k, br in enumerate(branches) if br['to'] == i)
        in_Q = sum(Q_ij[k] - br['x']*l_ij[k] for k, br in enumerate(branches) if br['to'] == i)
        
        constraints.extend([P_g - P_l == out_P - in_P, Q_g - Q_l == out_Q - in_Q])
        
    # 5. Objective
    obj = 0
    weights = {2: 1, 3: 10, 4: 100}
    for k, l_dict in enumerate(live_loads):
        obj += weights.get(l_dict.get('priority', 2), 1) * P_shed[k]
        
    prob = cp.Problem(cp.Minimize(obj), constraints)
    try:
        prob.solve(verbose=False)
    except Exception as e:
        print("SOCP SOLVE EXCEPTION:", e)
        
    shed_set = set()
    if prob.status in ["optimal", "optimal_inaccurate"]:
        for k, l_dict in enumerate(live_loads):
            val = P_shed[k].value
            if val is not None and (val * 100.0) > (l_dict['mw'] * 0.5): # Thresholding kontinyu -> diskrit
                shed_set.add(l_dict['name'])
                
    return shed_set, prob.status
 
def solve_feeder_contingency_shedding(gen_statuses, live_loads, current_tripped, line_statuses, viol_buses):
    """
    Targeted Locational Sensitivity-based Load Shedding untuk Feeder Contingency:
    1. Memprioritaskan beban di gardu yang kehilangan suplai transmisi langsung (Locational Proximity / SPS).
    2. Menguji beban Prioritas 2 (non-esensial) terbesar terlebih dahulu secara selektif.
    3. Jika pemotongan 1 beban tunggal cukup mengembalikan sistem ke kondisi aman (<= 85%),
       hanya beban tersebut yang dilepas (mencegah over-shedding massal).
    4. Jika 1 beban belum cukup, menguji pasangan 2 beban (mengutamakan beban lokal terlebih dahulu).
    5. Jika Prioritas 2 habis, baru merambah ke Prioritas 3.
    6. Fallback ke SOCP OPF jika pencarian kombinasi minimal tidak konvergen.
    """
    candidates = [l for l in live_loads if l['name'] not in current_tripped]

    # Identifikasi gardu lokal yang terdampak langsung oleh kawat transmisi yang putus
    local_prefix = ''
    if line_statuses:
        if line_statuses.get('Line_1-1') == 'OFFLINE':
            local_prefix = 'L3'  # Gardu Induk N3 (Sisi Kiri)
        elif line_statuses.get('Line_1-2') == 'OFFLINE':
            local_prefix = 'L4'  # Gardu Induk N4 (Sisi Kanan)

    # Evaluasi beban Prioritas 2 (Non-Esensial), lalu jika belum cukup masuk ke Prioritas 3 (Esensial)
    for target_prio in [2, 3]:
        prio_cands = [l for l in candidates if l.get('priority', 2) == target_prio]
        # Sort: utamakan beban lokal gardu terdampak lebih dulu, lalu descending MW
        prio_cands.sort(key=lambda x: (1 if (local_prefix and x['name'].startswith(local_prefix)) else 0, x.get('actual_mw', x.get('mw', 0))), reverse=True)
        
        # 1. Coba 1 beban tunggal (terbesar lebih dulu)
        for cand in prio_cands:
            test_set = current_tripped | {cand['name']}
            valid, _ = verify_ac_post_shedding(gen_statuses, live_loads, test_set, line_statuses)
            if valid:
                return test_set, f"Targeted Single ({cand['name']})"
                
        # 2. Coba pasangan 2 beban jika 1 beban belum cukup
        # Urutkan pasangan: utamakan yang memotong beban lokal terbanyak, lalu total MW minimal (hemat energi)
        pairs = list(itertools.combinations(prio_cands, 2))
        def pair_score(pair):
            loc_count = sum(1 for x in pair if local_prefix and x['name'].startswith(local_prefix))
            total_mw = sum(x.get('actual_mw', x.get('mw', 0)) for x in pair)
            return (-loc_count, total_mw)
            
        pairs.sort(key=pair_score)

        for pair in pairs:
            test_set = current_tripped | {pair[0]['name'], pair[1]['name']}
            valid, _ = verify_ac_post_shedding(gen_statuses, live_loads, test_set, line_statuses)
            if valid:
                return test_set, f"Targeted Pair ({pair[0]['name']}+{pair[1]['name']})"

    # Fallback ke SOCP OPF jika pencarian kombinasi minimal tidak konvergen
    shed_socp, socp_st = solve_socp_fallback(gen_statuses, live_loads, current_tripped, line_statuses)
    return shed_socp, "SOCP Fallback"


