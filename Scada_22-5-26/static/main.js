// ==========================================
// SCADA HMI FRONTEND LOGIC
// ==========================================

let socket;
let freqChart;

const urlParams = new URLSearchParams(window.location.search);
const IS_ADMIN = urlParams.get('role') === 'admin';

// --- Welcome Screen Logic ---
function promptAdmin() {
    document.getElementById('admin-modal').style.display = 'block';
}

function enterDashboard(role) {
    if (role === 'admin') {
        const pwd = document.getElementById('admin-pwd').value;
        if (pwd !== 'admin123') {
            alert('Password salah!');
            return;
        }
        window.location.href = '/?role=admin'; 
    } else {
        const overlay = document.getElementById('welcome-screen');
        if(overlay) {
            overlay.style.opacity = '0';
            overlay.style.transform = 'translateY(-20px)';
            setTimeout(() => overlay.style.display = 'none', 800);
        }
        
        if (IS_ADMIN) {
            window.history.pushState({}, '', '/');
            window.location.reload();
        }
    }
}

document.addEventListener('DOMContentLoaded', () => {
    // Welcome Screen Init
    const welcomeScreen = document.getElementById('welcome-screen');
    if (welcomeScreen && IS_ADMIN) {
        welcomeScreen.style.display = 'none';
    }

    init3DBackground();
    initSocket();
    initTabs();
    initFreqChart();
    initOverrides();
    initSLD();
});

// --- 3D Background ---
function init3DBackground() {
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(75, window.innerWidth / window.innerHeight, 0.1, 1000);
    const renderer = new THREE.WebGLRenderer({ canvas: document.getElementById('bg-canvas'), alpha: true });
    
    renderer.setSize(window.innerWidth, window.innerHeight);
    camera.position.z = 50;

    const particles = new THREE.BufferGeometry();
    const count = 1000;
    const positions = new Float32Array(count * 3);
    for(let i=0; i<count*3; i++) {
        positions[i] = (Math.random() - 0.5) * 200;
    }
    particles.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    
    const material = new THREE.PointsMaterial({
        size: 0.15,
        color: 0x00f3ff,
        transparent: true,
        opacity: 0.4
    });
    
    const particleSystem = new THREE.Points(particles, material);
    scene.add(particleSystem);

    function animate() {
        requestAnimationFrame(animate);
        particleSystem.rotation.y += 0.0005;
        particleSystem.rotation.x += 0.0002;
        renderer.render(scene, camera);
    }
    animate();
    
    window.addEventListener('resize', () => {
        camera.aspect = window.innerWidth / window.innerHeight;
        camera.updateProjectionMatrix();
        renderer.setSize(window.innerWidth, window.innerHeight);
    });
}

// --- TABS NAVIGATION ---
function initTabs() {
    const links = document.querySelectorAll('.nav-item');
    const panes = document.querySelectorAll('.tab-pane');
    
    links.forEach(link => {
        link.addEventListener('click', (e) => {
            e.preventDefault();
            const target = link.getAttribute('data-tab');
            
            links.forEach(l => l.classList.remove('active'));
            link.classList.add('active');
            
            panes.forEach(p => p.classList.remove('active'));
            document.getElementById(target).classList.add('active');
        });
    });
}

// --- FREQUENCY CHART ---
function initFreqChart() {
    const ctx = document.getElementById('freqChart').getContext('2d');
    freqChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: Array(120).fill(''),
            datasets: [{
                label: 'System Frequency (Hz)',
                data: Array(120).fill(50.0),
                borderColor: '#00f3ff',
                backgroundColor: 'rgba(0, 243, 255, 0.1)',
                borderWidth: 2,
                fill: true,
                tension: 0.4,
                pointRadius: 0
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                y: { min: 48.0, max: 52.0, grid: { color: 'rgba(255,255,255,0.05)' }, ticks: { color: '#94a3b8' } },
                x: { display: false }
            },
            plugins: { legend: { display: false } },
            animation: { duration: 0 }
        }
    });
}

// --- SOCKET CONNECTION & UPDATES ---
function initSocket() {
    socket = io();

    socket.on('connect', () => {
        document.getElementById('plc-status').className = 'status-pill online';
        document.getElementById('plc-status').innerHTML = '<i class="ph-fill ph-check-circle"></i> PLC ONLINE';
    });

    socket.on('disconnect', () => {
        document.getElementById('plc-status').className = 'status-pill offline';
        document.getElementById('plc-status').innerHTML = '<i class="ph-fill ph-warning-circle"></i> PLC OFFLINE';
    });

    socket.on('grid_update', (data) => {
        updateOverview(data);
        updateGenerators(data);
        updateLoads(data);
        updateContingency(data);
        updateSLD(data);
        logAlarm(data.log);
    });
}

// --- INTERACTIVE SLD ---
let sldScale = 1;
let isDraggingSLD = false;
let startX, startY, translateX = 0, translateY = 0;

function initSLD() {
    fetch('/static/SLD.svg')
        .then(res => res.text())
        .then(svgText => {
            const container = document.getElementById('sld-container');
            container.innerHTML = svgText;
            
            const svgEl = container.querySelector('svg');
            svgEl.style.width = '100%';
            svgEl.style.height = '100%';
            
            // Ubah garis default (hitam) menjadi warna abu-abu kebiruan (metalik) agar cocok dengan dark mode
            svgEl.querySelectorAll('polyline, path, polygon, ellipse').forEach(el => {
                if (el.getAttribute('stroke') === '#000000') el.setAttribute('stroke', '#475569'); 
                if (el.getAttribute('fill') === '#000000') el.setAttribute('fill', '#475569');
            });
            // Ubah teks default (hitam) menjadi terang
            svgEl.querySelectorAll('text').forEach(el => {
                if (el.getAttribute('fill') === '#000000') el.setAttribute('fill', '#e2e8f0');
            });
            
            // Pan & Zoom Listeners
            container.addEventListener('wheel', (e) => {
                e.preventDefault();
                window.zoomSLD(e.deltaY > 0 ? -0.1 : 0.1);
            });
            container.addEventListener('mousedown', (e) => {
                isDraggingSLD = true;
                startX = e.clientX - translateX;
                startY = e.clientY - translateY;
                container.style.cursor = 'grabbing';
            });
            window.addEventListener('mouseup', () => {
                isDraggingSLD = false;
                container.style.cursor = 'grab';
            });
            window.addEventListener('mousemove', (e) => {
                if(!isDraggingSLD) return;
                translateX = e.clientX - startX;
                translateY = e.clientY - startY;
                applyTransform();
            });
        });
}

window.zoomSLD = function(delta) {
    sldScale = Math.max(0.5, Math.min(sldScale + delta, 4));
    applyTransform();
}
window.resetSLD = function() {
    sldScale = 1; translateX = 0; translateY = 0;
    applyTransform();
}
function applyTransform() {
    document.getElementById('sld-container').style.transform = `translate(${translateX}px, ${translateY}px) scale(${sldScale})`;
}

function updateSLD(data) {
    const container = document.getElementById('sld-container');
    const svg = container.querySelector('svg');
    if(!svg) return; // belum loading
    
    function colorize(groupId, color, shadowColor, addFlow) {
        const gMain = svg.getElementById(groupId);
        const gAttr = svg.getElementById(groupId + '::LineAttribs');
        
        [gMain, gAttr].forEach(g => {
            if(!g) return;
            g.querySelectorAll('polyline, polygon, ellipse, path').forEach(el => {
                if(el.getAttribute('stroke') && el.getAttribute('stroke') !== 'none') {
                    el.setAttribute('stroke', color);
                }
                if(el.getAttribute('fill') && el.getAttribute('fill') !== 'none') {
                    el.setAttribute('fill', color);
                }
                
                // Animasi aliran listrik hanya pada garis/polyline
                if (el.tagName.toLowerCase() === 'polyline' || el.tagName.toLowerCase() === 'path') {
                    if (addFlow) el.classList.add('energy-flow');
                    else el.classList.remove('energy-flow');
                }
                
                // Tambahkan efek glow (kecuali kalau elemen ellipse sudah dihandle gen-pulse)
                if (!el.classList.contains('gen-pulse')) {
                    el.style.filter = `drop-shadow(0 0 5px ${shadowColor})`;
                }
            });
        });
    }

    let isGridActive = data.total_gen > 0;

    data.loads.forEach(l => {
        let isTripped = l.status === 'TRIPPED';
        let color = (!isGridActive || isTripped) ? '#ff4444' : '#00ffaa'; 
        let shadow = (!isGridActive || isTripped) ? 'rgba(255,68,68,0.6)' : 'rgba(0,255,170,0.6)';
        let flow = isGridActive && !isTripped;
        colorize(`New\\${l.id}.ElmLod`, color, shadow, flow);
    });
    
    let isBus1Active = false;
    let isBus2Active = false;
    
    data.generators.forEach(g => {
        let isTripped = g.status === 'OFFLINE';
        if (!isTripped) {
            if (g.name === 'PLTA' || g.name === 'PLTS') isBus1Active = true;
            if (g.name === 'PLTGU' || g.name === 'PLTB') isBus2Active = true;
        }
        
        let color = isTripped ? '#ff4444' : '#00ffaa';
        let shadow = isTripped ? 'rgba(255,68,68,0.6)' : 'rgba(0,255,170,0.6)';
        let svgName = g.name;
        
        colorize(`New\\${svgName}.ElmSym`, color, shadow, !isTripped);
        
        // Add specific generator animations
        let gMain = svg.getElementById(`New\\${svgName}.ElmSym`);
        if(gMain) {
            let ellipse = gMain.querySelector('ellipse');
            if(ellipse) {
                if(!isTripped) {
                    ellipse.classList.add('gen-pulse');
                    ellipse.style.filter = ''; // Let CSS keyframes handle the glow
                } else {
                    ellipse.classList.remove('gen-pulse');
                    ellipse.style.filter = `drop-shadow(0 0 5px ${shadow})`; // Static red glow
                }
            }
            
            gMain.querySelectorAll('text').forEach(t => {
                if(t.textContent.trim() === '~') {
                    if(!isTripped) t.classList.add('gen-spin');
                    else t.classList.remove('gen-spin');
                }
            });
        }
    });

    // Topology-aware grid lighting
    const backboneGroups = [
        'Line_1.ElmLne', 'Line_2.ElmLne', 'Line 1-2.ElmLne', 'Line_1-1.ElmLne', 
        'N1_SS1.ElmTerm', 'N2_SS2.ElmTerm', 'N3_SS3.ElmTerm', 'N4_SS4_150kV.ElmTerm', 'N5_SS4_20kV.ElmTerm', 
        'Tranformator.ElmTr2'
    ];
    
    backboneGroups.forEach(id => {
        let color = isGridActive ? '#00ffaa' : '#475569';
        let shadow = isGridActive ? 'rgba(0,255,170,0.6)' : 'transparent';
        colorize(`New\\${id}`, color, shadow, isGridActive);
    });

    // Generator Bus 1 (PLTA & PLTS)
    ['150kV-1.ElmTerm', 'Tranformator_1.ElmTr2'].forEach(id => {
        let color = isBus1Active ? '#00ffaa' : '#475569';
        let shadow = isBus1Active ? 'rgba(0,255,170,0.6)' : 'transparent';
        colorize(`New\\${id}`, color, shadow, isBus1Active);
    });

    // Generator Bus 2 (PLTGU & PLTB)
    ['150kV-2.ElmTerm', 'Tranformator_2.ElmTr2'].forEach(id => {
        let color = isBus2Active ? '#00ffaa' : '#475569';
        let shadow = isBus2Active ? 'rgba(0,255,170,0.6)' : 'transparent';
        colorize(`New\\${id}`, color, shadow, isBus2Active);
    });
}

function updateOverview(data) {
    // Top Bar
    document.getElementById('sys-time').innerText = data.plc_time;
    
    // Cards
    document.getElementById('ov-gen').innerText = data.total_gen.toFixed(1);
    document.getElementById('ov-load').innerText = data.total_load.toFixed(1);
    document.getElementById('ov-def').innerText = data.deficit.toFixed(1);
    
    let f = data.frequency;
    
    const freqCard = document.getElementById('card-freq');
    if (f === 0) {
        document.getElementById('ov-freq').innerText = "-";
        freqCard.className = 'metric-card glass-panel';
        freqChart.data.datasets[0].data.push(null);
    } else {
        document.getElementById('ov-freq').innerText = f.toFixed(2);
        if(f < 49.5 || f > 50.5) { freqCard.className = 'metric-card glass-panel danger'; }
        else if(f < 49.8 || f > 50.2) { freqCard.className = 'metric-card glass-panel warning'; }
        else { freqCard.className = 'metric-card glass-panel'; }
        freqChart.data.datasets[0].data.push(f);
    }

    freqChart.data.datasets[0].data.shift();
    freqChart.update();
}

function updateGenerators(data) {
    const tbody = document.getElementById('gen-table-body');
    const container = document.getElementById('gen-bars-container');
    
    let tableHtml = '';
    let barsHtml = '';
    
    data.generators.forEach(g => {
        const pct = (g.mw / g.rated) * 100;
        const color = g.status === 'ONLINE' ? 'var(--primary-blue)' : 'var(--text-muted)';
        const btnAction = g.status === 'ONLINE' ? 'OFF' : 'ON';
        const btnClass = g.status === 'ONLINE' ? 'btn danger' : 'btn';
        
        tableHtml += `
            <tr>
                <td>${g.name} <br><small style="color:var(--text-muted)">${g.type.toUpperCase()}</small></td>
                <td>${g.rated} MW</td>
                <td>${g.mw} MW</td>
                <td><span class="badge ${g.status.toLowerCase()}">${g.status}</span></td>
                <td>
                    <button class="${btnClass}" onclick="toggleGen('${g.name}', '${btnAction}')">${btnAction}</button>
                </td>
            </tr>
        `;
        
        barsHtml += `
            <div class="gen-bar-wrapper">
                <div class="gen-bar-label">
                    <span>${g.name} (${g.status})</span>
                    <span>${g.mw} / ${g.rated} MW</span>
                </div>
                <div class="gen-bar-track">
                    <div class="gen-bar-fill" style="width: ${pct}%; background: ${color}"></div>
                </div>
            </div>
        `;
    });
    
    if(tbody) tbody.innerHTML = tableHtml;
    if(container) container.innerHTML = barsHtml;
}

function updateLoads(data) {
    const tbody = document.getElementById('load-table-body');
    let html = '';
    data.loads.forEach(l => {
        html += `
            <tr>
                <td>${l.id}</td>
                <td>${l.mw} MW</td>
                <td><span class="badge ${l.status === 'TRIPPED' ? 'offline' : 'online'}">${l.status}</span></td>
            </tr>
        `;
    });
    if(tbody) tbody.innerHTML = html;
}

function logAlarm(msg) {
    if(!msg) return;
    const logDiv = document.getElementById('alarm-log-content');
    if(!logDiv) return;
    
    const div = document.createElement('div');
    div.style.padding = '8px 0';
    div.style.borderBottom = '1px solid var(--glass-border)';
    div.style.fontFamily = 'var(--font-mono)';
    div.style.fontSize = '0.85rem';
    
    if(msg.includes('DEFISIT') || msg.includes('TRIPPED')) {
        div.style.color = 'var(--danger-red)';
    } else if (msg.includes('RESTORASI')) {
        div.style.color = 'var(--success-green)';
    }
    
    div.innerText = msg;
    logDiv.prepend(div);
    if(logDiv.children.length > 50) logDiv.lastChild.remove();
}

// --- CONTINGENCY MATRIX ---
function updateContingency(data) {
    const tbody = document.getElementById('contingency-body');
    if(!tbody || !data.contingency || !data.loads) return;
    
    let html = '';
    const genNames = ['PLTA', 'PLTS', 'PLTGU', 'PLTB'];
    
    data.loads.forEach(load => {
        html += `<tr>`;
        html += `<td style="text-align: left; font-weight: 500;">${load.id}</td>`;
        html += `<td style="font-family: var(--font-mono); color: var(--text-muted);">${load.mw} MW</td>`;
        
        genNames.forEach(gen => {
            // Prediksi shed load untuk generator ini (kalau gen mati)
            const shedList = data.contingency[gen] || [];
            const isShed = shedList.includes(load.id);
            
            if (isShed) {
                // Red block
                html += `<td style="background-color: #ff0000; opacity: 0.9;"></td>`;
            } else {
                html += `<td></td>`;
            }
        });
        
        html += `</tr>`;
    });
    tbody.innerHTML = html;
}

// --- OVERRIDES (ADMIN ONLY) ---
const LOAD_NAMES = ['L101','L201','L303','L304','L305','L401','L301','L302','L402','L403','L404','L405'];
const MAX_LOADS  = [ 20,    20,    15,    20,    30,    30,    5,     10,    5,     10,    15,    20  ];

function initOverrides() {
    const role = new URLSearchParams(window.location.search).get('role') || 'viewer';
    document.getElementById('role-badge').innerText = role.toUpperCase();
    
    if(role === 'admin') {
        document.querySelectorAll('.admin-only').forEach(el => el.style.display = '');
    } else {
        document.querySelectorAll('.admin-only').forEach(el => el.style.display = 'none');
    }
        
    const container = document.getElementById('override-inputs');
    if(container) {
        let html = '';
        LOAD_NAMES.forEach((name, i) => {
            html += `
                <div style="display:flex; justify-content:space-between; align-items:center; background: rgba(0,0,0,0.2); padding: 6px 12px; border-radius: 6px; border: 1px solid var(--glass-border);">
                    <span style="font-family: var(--font-mono); font-size: 0.9rem; font-weight: bold; width: 50px;">${name}</span>
                    
                    <div style="display: flex; gap: 8px; align-items: center;">
                        <select id="mode-${i}" onchange="toggleMode(${i})" style="background: rgba(255,255,255,0.1); color: #fff; border: 1px solid var(--glass-border); border-radius: 4px; padding: 4px; font-family: var(--font-sans); outline: none;">
                            <option value="AUTO" style="color:#000">AUTO (Sensor)</option>
                            <option value="MANUAL" style="color:#000">MANUAL</option>
                        </select>
                        
                        <input type="number" id="ov-${i}" value="0" min="0" max="${MAX_LOADS[i]}" disabled style="width: 50px; background: rgba(0,0,0,0.3); border: 1px solid var(--glass-border); border-radius: 4px; color: var(--text-muted); font-family: var(--font-mono); text-align: right; padding: 4px; transition: all 0.3s;">
                        <span style="font-size: 0.8rem; color: var(--text-muted); width: 45px;">/ ${MAX_LOADS[i]} MW</span>
                    </div>
                </div>
            `;
        });
        container.innerHTML = html;
    }
}

window.toggleMode = function(i) {
    const mode = document.getElementById(`mode-${i}`).value;
    const input = document.getElementById(`ov-${i}`);
    if(mode === 'AUTO') {
        input.disabled = true;
        input.style.color = 'var(--text-muted)';
        input.style.borderColor = 'var(--glass-border)';
    } else {
        input.disabled = false;
        input.style.color = 'var(--text-main)';
        input.style.borderColor = 'var(--primary-blue)';
        if(input.value == 0) input.value = 20; // Default dummy value
    }
};

function sendOverrides() {
    if(!socket) return;
    const vals = [];
    for(let i=0; i<12; i++) {
        const mode = document.getElementById(`mode-${i}`).value;
        const input = document.getElementById(`ov-${i}`);
        if(mode === 'AUTO') {
            vals.push(0); // 0 means AUTO to load.py
        } else {
            vals.push(parseInt(input.value) || 0);
        }
    }
    socket.emit('set_load_interrupt', { loads: vals });
    alert("Sensor Override Configuration sent to PLC!");
}

// --- CONTROLS ---
function toggleGen(name, action) {
    if(!socket) return;
    socket.emit('gen_control', { name, action });
}
