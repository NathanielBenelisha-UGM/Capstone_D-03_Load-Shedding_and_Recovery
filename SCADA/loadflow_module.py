import pandapower as pp
import pulp
import pandas as pd
import os

def solve_milp_shedding(deficit, live_loads, current_tripped=set()):
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

    # Lines (max_i_ka diturunkan lagi ke 0.2 kA / ~52 MVA agar lebih mudah mencapai >80% loading)
    line_length = 20.0 
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

    # Breaker Intertrip Logic (Putus Trafo jika bus generator mati total)
    bus1_active = any(g['status'] == 'ONLINE' for g in gen_statuses if g['name'] in ['PLTA', 'PLTS'])
    bus2_active = any(g['status'] == 'ONLINE' for g in gen_statuses if g['name'] in ['PLTGU', 'PLTB'])
    
    if not bus1_active:
        idx_t1 = net.trafo[net.trafo.name == "Trafo 1"].index
        if len(idx_t1) > 0:
            net.trafo.loc[idx_t1[0], 'in_service'] = False
            
    if not bus2_active:
        idx_t2 = net.trafo[net.trafo.name == "Trafo 2"].index
        if len(idx_t2) > 0:
            net.trafo.loc[idx_t2[0], 'in_service'] = False

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
