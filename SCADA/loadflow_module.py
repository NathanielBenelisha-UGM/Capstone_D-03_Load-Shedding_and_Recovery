import pandapower as pp
import pulp
import pandas as pd
import os
import cvxpy as cp
import numpy as np

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

    # Lines (panjang kabel diperbesar agar voltage drop lebih mudah terjadi)
    line_length = 60.0
    pp.create_line_from_parameters(net, b_150_1, b_150_2, length_km=line_length, r_ohm_per_km=0.1, x_ohm_per_km=0.4, c_nf_per_km=10., max_i_ka=0.2, name="Line_1")
    pp.create_line_from_parameters(net, b_150_1, b_150_3, length_km=line_length, r_ohm_per_km=0.1, x_ohm_per_km=0.4, c_nf_per_km=10., max_i_ka=0.3, name="Line_1-1")
    pp.create_line_from_parameters(net, b_150_2, b_150_4, length_km=line_length, r_ohm_per_km=0.1, x_ohm_per_km=0.4, c_nf_per_km=10., max_i_ka=0.3, name="Line_1-2")
    pp.create_line_from_parameters(net, b_150_3, b_150_4, length_km=line_length, r_ohm_per_km=0.1, x_ohm_per_km=0.4, c_nf_per_km=10., max_i_ka=0.2, name="Line_2")

    # Generators
    pp.create_ext_grid(net, bus=b_66_1, vm_pu=1.0, name="Slack (GEN_1A PLTA)")
    pp.create_sgen(net, bus=b_66_1, p_mw=25.0, name="GEN_1B PLTS")
    
    pp.create_gen(net, bus=b_66_2, p_mw=75.0, vm_pu=1.0, name="GEN_2A PLTGU")
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
        pp.create_load(net, bus=ld['bus'], p_mw=ld['p_mw'], name=ld['name'])
        
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
    
    # Check max overload
    max_line = l_load['loading_percent'].max()
    max_trafo = t_load['loading_percent'].max()
    max_loading = max(max_line, max_trafo)
    
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
        'min_v': float(min_v),
        'max_v': float(max_v)
    }

def run_live_loadflow(gen_statuses, live_loads, tripped_loads):
    """
    Menjalankan pandapower berdasarkan status aktual SCADA
    gen_statuses: list of dict {'name': 'PLTA', 'status': 'ONLINE', 'mw': ...}
    live_loads: list of dict {'name': 'L101', 'mw': ...}
    tripped_loads: set/list of string load names yang mati
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
            # Gunakan actual_mw agar load flow sama persis dengan angka real-time sensor SCADA
            current_p = l.get('actual_mw', l['mw'])
            net.load.loc[idx_load[0], 'p_mw'] = current_p if current_p > 0 else 0.1

    # Ensure at least one slack bus is active (prevent 'No reference bus' error)
    if net.ext_grid[net.ext_grid.in_service == True].empty:
        active_gens = net.gen[net.gen.in_service == True]
        if not active_gens.empty:
            gen_idx = active_gens.index[0]
            bus_idx = net.gen.loc[gen_idx, 'bus']
            gen_name = net.gen.loc[gen_idx, 'name']
            net.gen.loc[gen_idx, 'in_service'] = False # Disable gen, replace with ext_grid
            pp.create_ext_grid(net, bus=bus_idx, vm_pu=1.0, name=f"Slack ({gen_name})")
        else:
            active_sgens = net.sgen[net.sgen.in_service == True]
            if not active_sgens.empty:
                sgen_idx = active_sgens.index[0]
                bus_idx = net.sgen.loc[sgen_idx, 'bus']
                sgen_name = net.sgen.loc[sgen_idx, 'name']
                net.sgen.loc[sgen_idx, 'in_service'] = False
                pp.create_ext_grid(net, bus=bus_idx, vm_pu=1.0, name=f"Slack ({sgen_name})")
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

def verify_ac_post_shedding(gen_statuses, live_loads, proposed_shed_set):
    """
    Menjalankan simulasi AC Load Flow untuk memverifikasi tegangan
    setelah MILP mengusulkan pemutusan beban.
    Returns: (is_valid, list_of_violating_bus_names)
    """
    res = run_live_loadflow(gen_statuses, live_loads, proposed_shed_set)
    if res.get('status') == 'error':
        return False, ["ALL_BUSES"] # Gagal konvergen
        
    buses = res['buses']
    violating_buses = []
    for b in buses:
        # Batas tegangan kritis diperketat: 0.95 pu - 1.05 pu
        if b['vm_pu'] < 0.95 or b['vm_pu'] > 1.05:
            violating_buses.append(b['index'])
            
    return len(violating_buses) == 0, violating_buses

def solve_socp_fallback(gen_statuses, live_loads, current_tripped=set()):
    """
    Matematika murni SOCP (Second-Order Cone Programming)
    Menggunakan Branch Flow Model (DistFlow Relaxation) via CVXPY.
    """
    net, _ = create_network()
    
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
        P_g = cp.Variable() if i == slack_idx else 0
        Q_g = cp.Variable() if i == slack_idx else 0
        if i == slack_idx:
            constraints.extend([P_g >= 0, P_g <= 0.75, Q_g >= -0.5, Q_g <= 0.5])
            
        for _, row in net.gen.iterrows():
            if bus_map[row.bus] == i and row.in_service and i != slack_idx: 
                P_g += row.p_mw / 100.0
        for _, row in net.sgen.iterrows():
            if bus_map[row.bus] == i and row.in_service and i != slack_idx: 
                P_g += row.p_mw / 100.0
            
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
        prob.solve(solver=cp.ECOS, verbose=False)
    except:
        pass
        
    shed_set = set()
    if prob.status in ["optimal", "optimal_inaccurate"]:
        for k, l_dict in enumerate(live_loads):
            val = P_shed[k].value
            if val is not None and (val * 100.0) > (l_dict['mw'] * 0.5): # Thresholding kontinyu -> diskrit
                shed_set.add(l_dict['name'])
                
    return shed_set, prob.status

