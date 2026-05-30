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
        if (overlay) {
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
    for (let i = 0; i < count * 3; i++) {
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

// --- SVG SLD LOADING & PAN/ZOOM ---
function loadSVG() {
    fetch('/static/SLD.svg').then(res => res.text()).then(svgText => {
        const sldContainer = document.getElementById('sld-container');
        if (sldContainer) sldContainer.innerHTML = svgText;

        initPanZoom('sld-container');
    });
}

function initPanZoom(containerId) {
    const container = document.getElementById(containerId);
    if (!container) return;
    const svg = container.querySelector('svg');
    if (!svg) return;

    let isDragging = false;
    let startX, startY;

    // Check if we already have transform data attached
    if (!container.dataset.scale) {
        container.dataset.scale = 1;
        container.dataset.translateX = 0;
        container.dataset.translateY = 0;
    }

    container.addEventListener('mousedown', (e) => {
        isDragging = true;
        startX = e.clientX - parseFloat(container.dataset.translateX);
        startY = e.clientY - parseFloat(container.dataset.translateY);
        container.style.cursor = 'grabbing';
    });

    window.addEventListener('mouseup', () => {
        isDragging = false;
        container.style.cursor = 'grab';
    });

    window.addEventListener('mousemove', (e) => {
        if (!isDragging) return;
        e.preventDefault();
        container.dataset.translateX = e.clientX - startX;
        container.dataset.translateY = e.clientY - startY;
        applyTransform(container);
    });

    container.addEventListener('wheel', (e) => {
        e.preventDefault();
        const delta = e.deltaY > 0 ? -0.1 : 0.1;
        let scale = parseFloat(container.dataset.scale);
        scale = Math.max(0.5, Math.min(3, scale + delta));
        container.dataset.scale = scale;
        applyTransform(container);
    });
}

function applyTransform(container) {
    const svg = container.querySelector('svg');
    if (svg) {
        svg.style.transform = `translate(${container.dataset.translateX}px, ${container.dataset.translateY}px) scale(${container.dataset.scale})`;
    }
}

window.zoomSLD = function (delta) {
    const container = document.getElementById('sld-container');
    if (!container) return;
    let scale = parseFloat(container.dataset.scale) + delta;
    container.dataset.scale = Math.max(0.5, Math.min(3, scale));
    applyTransform(container);
};

window.resetSLD = function () {
    const container = document.getElementById('sld-container');
    if (!container) return;
    container.dataset.scale = 1;
    container.dataset.translateX = 0;
    container.dataset.translateY = 0;
    applyTransform(container);
};

window.lastLoadflowData = null;

function applyHeatmapToSLD(d) {
    const hmContainer = document.getElementById('sld-container');
    if (!hmContainer) return;
    const svg = hmContainer.querySelector('svg');
    if (!svg) return;

    function colorizeHeatmap(id, loading, isBusbar=false, customColor=null) {
        let color = customColor || '#00ffaa'; // Green default
        if (!customColor) {
            if (loading > 100) color = '#ff4444';
            else if (loading > 80) color = '#ffca28';
        }

        let el = svg.getElementById(id);
        if (!el) return;

        el.querySelectorAll('polyline, polygon, ellipse, path').forEach(child => {
            if (child.getAttribute('stroke') && child.getAttribute('stroke') !== 'none') child.setAttribute('stroke', color);
            if (child.tagName === 'polygon' && child.getAttribute('fill') !== 'none') child.setAttribute('fill', color);
            child.style.transition = 'all 0.3s ease';
            if ((loading > 100 || isBusbar && customColor !== '#00ffaa') && !child.classList.contains('gen-pulse')) {
                child.style.filter = `drop-shadow(0 0 8px ${color})`;
                if(!isBusbar) child.style.strokeWidth = '4px';
            } else {
                child.style.filter = 'none';
                if(!isBusbar) child.style.strokeWidth = '1px';
            }
        });
    }

    if (d.lines) {
        d.lines.forEach(line => {
            let svgName = line.name;
            if (line.name === "Line_1-2") svgName = "Line 1-2";
            colorizeHeatmap(`New\\${svgName}.ElmLne`, line.loading_percent);
        });
    }

    if (d.trafos) {
        d.trafos.forEach(t => {
            let svgName = "Tranformator";
            if (t.name === "Trafo 1") svgName = "Tranformator_1";
            if (t.name === "Trafo 2") svgName = "Tranformator_2";
            colorizeHeatmap(`New\\${svgName}.ElmTr2`, t.loading_percent);
        });
    }

    if (d.buses) {
        d.buses.forEach(b => {
            let busName = b.name || b.index;
            let color = '#00ffaa'; // Normal
            if (b.vm_pu > 1.02 || b.vm_pu < 0.99) color = '#ffca28'; // Warning (Tegangan mulai tidak ideal)
            if (b.vm_pu > 1.05 || b.vm_pu < 0.95) color = '#ff4444'; // Danger (Tegangan kritis)
            
            let svgId = null;
            if (busName.includes('N1_SS1')) svgId = 'New\\N1_SS1.ElmTerm';
            else if (busName.includes('N2_SS2')) svgId = 'New\\N2_SS2.ElmTerm';
            else if (busName.includes('N3_SS3')) svgId = 'New\\N3_SS3.ElmTerm';
            else if (busName.includes('N4_SS4_150kV')) svgId = 'New\\N4_SS4_150kV.ElmTerm';
            else if (busName.includes('N5_SS4_20kV')) svgId = 'New\\N5_SS4_20kV.ElmTerm';
            else if (busName.includes('66kV-1')) svgId = 'New\\66kV-1.ElmTerm';
            else if (busName.includes('66kV-2')) svgId = 'New\\66kV-2.ElmTerm';
            
            if (svgId) colorizeHeatmap(svgId, 0, true, color);
        });
    }
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
        if (window.lastLoadflowData) applyHeatmapToSLD(window.lastLoadflowData);
        logAlarm(data.log);
    });

    socket.on('live_loadflow_result', (res) => {
        const badge = document.getElementById('lf-status-badge');

        if (badge) {
            badge.innerHTML = '<i class="ph-fill ph-check-circle"></i> Live Data Received';
            setTimeout(() => { badge.innerHTML = '<i class="ph ph-spinner ph-spin"></i> Waiting for Data...'; }, 2000);
        }

        if (res.status === 'error') {
            document.getElementById('lf-status-text').innerText = 'ERROR: ' + res.message;
            document.getElementById('lf-status-text').style.color = '#ff4444';
            return;
        }

        const d = res.data;
        document.getElementById('lf-max-loading').innerText = d.max_loading + '%';
        document.getElementById('lf-max-loading').style.color = d.max_loading > 100 ? '#ff4444' : (d.max_loading > 80 ? '#ffca28' : '#00ffaa');

        document.getElementById('lf-min-voltage').innerText = d.min_v + ' pu';
        document.getElementById('lf-min-voltage').style.color = d.min_v < 0.95 ? '#ff4444' : (d.min_v < 0.99 ? '#ffca28' : '#00ffaa');

        if (d.slack_name) {
            let shortName = d.slack_name.replace("Slack (", "").replace(")", "");
            document.getElementById('lf-slack-title').innerText = `Slack Bus Output (${shortName})`;
            document.getElementById('lf-slack-capacity').innerText = `Kapasitas Aman: ${d.slack_capacity.toFixed(1)} MW`;
            
            document.getElementById('lf-slack-mw').innerHTML = `${d.slack_mw.toFixed(1)}<span style="font-size: 1rem;">MW</span>`;
            document.getElementById('lf-slack-mw').style.color = d.slack_mw > d.slack_capacity ? '#ff4444' : (d.slack_mw > d.slack_capacity * 0.9 ? '#ffca28' : '#00ffaa');
        } else {
            document.getElementById('lf-slack-mw').innerHTML = `${d.slack_mw.toFixed(1)}<span style="font-size: 1rem;">MW</span>`;
            document.getElementById('lf-slack-mw').style.color = d.slack_mw > 75.0 ? '#ff4444' : (d.slack_mw > 65.0 ? '#ffca28' : '#00ffaa');
        }

        const statusText = document.getElementById('lf-status-text');
        if (d.max_loading > 100) {
            statusText.innerText = 'OVERLOAD DETECTED';
            statusText.style.color = '#ff4444';
        } else {
            statusText.innerText = 'STABLE';
            statusText.style.color = '#00ffaa';
        }

        window.lastLoadflowData = d;
        applyHeatmapToSLD(d);
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
                if (!isDraggingSLD) return;
                translateX = e.clientX - startX;
                translateY = e.clientY - startY;
                applyTransform();
            });
        });
}

window.zoomSLD = function (delta) {
    sldScale = Math.max(0.5, Math.min(sldScale + delta, 4));
    applyTransform();
}
window.resetSLD = function () {
    sldScale = 1; translateX = 0; translateY = 0;
    applyTransform();
}
function applyTransform() {
    document.getElementById('sld-container').style.transform = `translate(${translateX}px, ${translateY}px) scale(${sldScale})`;
}

function updateSLD(data) {
    const container = document.getElementById('sld-container');
    const svg = container.querySelector('svg');
    if (!svg) return; // belum loading

    function colorize(groupId, color, shadowColor, addFlow) {
        const gMain = svg.getElementById(groupId);
        const gAttr = svg.getElementById(groupId + '::LineAttribs');

        [gMain, gAttr].forEach(g => {
            if (!g) return;
            g.querySelectorAll('polyline, polygon, ellipse, path').forEach(el => {
                if (el.getAttribute('stroke') && el.getAttribute('stroke') !== 'none') {
                    el.setAttribute('stroke', color);
                }
                if (el.getAttribute('fill') && el.getAttribute('fill') !== 'none') {
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

        const gMain = svg.getElementById(`New\\${l.id}.ElmLod`);
        // Find text element globally just in case it's not inside gMain
        let dynText = svg.querySelector(`text.dynamic-data[data-id="${l.id}"]`);
        if (!dynText) {
            const origText = Array.from(svg.querySelectorAll('text')).find(t => !t.classList.contains('dynamic-data') && t.textContent.trim() === l.id);
            if (origText) {
                origText.setAttribute('fill', color);
                dynText = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                dynText.setAttribute('class', 'dynamic-data');
                dynText.setAttribute('data-id', l.id);
                dynText.setAttribute('x', origText.getAttribute('x') || 0);
                let origY = parseFloat(origText.getAttribute('y') || 0);
                dynText.setAttribute('y', origY + 12);
                dynText.setAttribute('font-size', '9');
                dynText.setAttribute('font-weight', 'bold');
                dynText.setAttribute('font-family', 'var(--font-mono)');

                // Append directly after origText so it shares same coordinate space if possible
                if (origText.parentNode) {
                    origText.parentNode.appendChild(dynText);
                }
            }
        }
        if (dynText) {
            let xPos = dynText.getAttribute('x') || 0;
            dynText.innerHTML = ''; // clear

            let tspan1 = document.createElementNS('http://www.w3.org/2000/svg', 'tspan');
            tspan1.setAttribute('x', xPos);
            tspan1.setAttribute('dy', '0');
            tspan1.textContent = `${l.mw} MW`;

            let tspan2 = document.createElementNS('http://www.w3.org/2000/svg', 'tspan');
            tspan2.setAttribute('x', xPos);
            tspan2.setAttribute('dy', '1.2em');
            tspan2.textContent = `Prio: ${l.priority}`;

            dynText.appendChild(tspan1);
            dynText.appendChild(tspan2);
            dynText.setAttribute('fill', '#fff');
        }
        // Also update original text color if it exists
        const origText = Array.from(svg.querySelectorAll('text')).find(t => !t.classList.contains('dynamic-data') && t.textContent.trim() === l.id);
        if (origText) origText.setAttribute('fill', color);
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
        if (gMain) {
            let ellipse = gMain.querySelector('ellipse');
            if (ellipse) {
                if (!isTripped) {
                    ellipse.classList.add('gen-pulse');
                    ellipse.style.filter = ''; // Let CSS keyframes handle the glow
                } else {
                    ellipse.classList.remove('gen-pulse');
                    ellipse.style.filter = `drop-shadow(0 0 5px ${shadow})`; // Static red glow
                }
            }

            gMain.querySelectorAll('text').forEach(t => {
                if (!t.classList.contains('dynamic-data') && t.textContent.trim() === '~') {
                    if (!isTripped) t.classList.add('gen-spin');
                    else t.classList.remove('gen-spin');
                }
            });
        }

        // Find generator text globally
        let dynText = svg.querySelector(`text.dynamic-data[data-id="${g.name}"]`);
        if (!dynText) {
            const origText = Array.from(svg.querySelectorAll('text')).find(t => !t.classList.contains('dynamic-data') && t.textContent.trim() === g.name);
            if (origText) {
                dynText = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                dynText.setAttribute('class', 'dynamic-data');
                dynText.setAttribute('data-id', g.name);
                dynText.setAttribute('x', origText.getAttribute('x') || 0);
                let origY = parseFloat(origText.getAttribute('y') || 0);
                dynText.setAttribute('y', origY + 16);
                dynText.setAttribute('font-size', '12');
                dynText.setAttribute('font-weight', 'bold');
                dynText.setAttribute('font-family', 'var(--font-mono)');

                if (origText.parentNode) {
                    origText.parentNode.appendChild(dynText);
                }
            }
        }
        if (dynText) {
            dynText.textContent = `${g.mw} MW`;
            dynText.setAttribute('fill', '#fff');
        }
        const origText = Array.from(svg.querySelectorAll('text')).find(t => !t.classList.contains('dynamic-data') && t.textContent.trim() === g.name);
        if (origText) origText.setAttribute('fill', color);
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
    ['66kV-1.ElmTerm', 'Tranformator_1.ElmTr2'].forEach(id => {
        let color = isBus1Active ? '#00ffaa' : '#475569';
        let shadow = isBus1Active ? 'rgba(0,255,170,0.6)' : 'transparent';
        colorize(`New\\${id}`, color, shadow, isBus1Active);
    });

    // Generator Bus 2 (PLTGU & PLTB)
    ['66kV-2.ElmTerm', 'Tranformator_2.ElmTr2'].forEach(id => {
        let color = isBus2Active ? '#00ffaa' : '#475569';
        let shadow = isBus2Active ? 'rgba(0,255,170,0.6)' : 'transparent';
        colorize(`New\\${id}`, color, shadow, isBus2Active);
    });
}

function updateContingency(data) {
    const tbody = document.getElementById('contingency-body');
    if (!tbody) return;

    let html = '';
    const genNames = ['PLTA', 'PLTS', 'PLTGU', 'PLTB'];

    data.loads.forEach(l => {
        let cells = '';
        genNames.forEach(gName => {
            const trippedLoads = data.contingency[gName] || [];
            const willTrip = trippedLoads.includes(l.id);
            if (willTrip) {
                cells += `<td style="background: rgba(255,42,95,0.2); color: var(--danger-red); font-weight: bold;">TRIP</td>`;
            } else {
                cells += `<td style="color: var(--text-muted);">-</td>`;
            }
        });

        html += `
            <tr>
                <td style="text-align: left; font-family: var(--font-mono); font-weight: bold;">${l.id}</td>
                <td style="font-family: var(--font-mono);">${l.mw} MW</td>
                ${cells}
            </tr>
        `;
    });

    tbody.innerHTML = html;
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
        if (f < 49.5 || f > 50.5) { freqCard.className = 'metric-card glass-panel danger'; }
        else if (f < 49.8 || f > 50.2) { freqCard.className = 'metric-card glass-panel warning'; }
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

    if (tbody) tbody.innerHTML = tableHtml;
    if (container) container.innerHTML = barsHtml;
}

const BUS_MAP = {
    'L101': 'N1_SS1', 'L201': 'N2_SS2',
    'L301': 'N3_SS3', 'L302': 'N3_SS3', 'L303': 'N3_SS3', 'L304': 'N3_SS3', 'L305': 'N3_SS3',
    'L401': 'N4_SS4', 'L402': 'N4_SS4', 'L403': 'N4_SS4', 'L404': 'N4_SS4', 'L405': 'N4_SS4'
};

function updateLoads(data) {
    const tbody = document.getElementById('load-table-body');
    const busTbody = document.getElementById('bus-priority-body');
    const showPriority = new URLSearchParams(window.location.search).get('role') === 'admin';

    let htmlLoad = '';
    const isBusTableEmpty = busTbody && busTbody.children.length === 0;
    let htmlBus = '';

    data.loads.forEach(l => {
        // --- 1. Basic Load Monitor Table ---
        htmlLoad += `
            <tr>
                <td>${l.id}</td>
                <td>${l.mw} MW</td>
                <td><span class="badge ${l.status === 'TRIPPED' ? 'offline' : 'online'}">${l.status}</span></td>
            </tr>`;

        // --- 2. Bus Grouping & Priority Table ---
        const prio = l.priority || 2;
        const defPrio = l.default_priority || 2;
        const prioClass = `prio-${prio}`;
        const defPrioClass = `prio-${defPrio}`;
        const prioLabel = { 2: 'LOW', 3: 'MED', 4: 'HIGH' }[prio] || 'LOW';
        const defPrioLabel = { 2: 'LOW', 3: 'MED', 4: 'HIGH' }[defPrio] || 'LOW';
        const busName = BUS_MAP[l.id] || 'Unknown';

        if (isBusTableEmpty) {
            const loadPrioCell = showPriority ? `
                <td style="text-align:center;">
                    <span class="priority-badge ${defPrioClass}">${defPrio} - ${defPrioLabel}</span>
                </td>` : '';

            const actualCell = showPriority ? `
                <td style="text-align:center;">
                    <span class="priority-badge ${prioClass}" id="prio-badge-${l.id}">${prio} - ${prioLabel}</span>
                </td>` : '';

            const operatorCell = showPriority ? `
                <td style="text-align:center;">
                    <input type="number" min="2" max="4" step="1"
                           class="priority-input"
                           id="prio-input-${l.id}"
                           value="${prio}"
                           title="2=Low  3=Med  4=High"
                           onchange="sendSinglePriority('${l.id}', this.value)"
                    >
                </td>` : '';

            htmlBus += `
                <tr>
                    <td style="font-weight:bold; color:var(--primary-blue)">${busName}</td>
                    <td style="font-family:var(--font-mono)">${l.id}</td>
                    ${loadPrioCell}
                    ${actualCell}
                    ${operatorCell}
                </tr>`;
        } else {
            // Hanya update badge agar tidak merusak input operator yg sedang diketik
            const badge = document.getElementById(`prio-badge-${l.id}`);
            if (badge) {
                badge.className = `priority-badge ${prioClass}`;
                badge.innerText = `${prio} - ${prioLabel}`;
            }
        }
    });

    if (tbody) tbody.innerHTML = htmlLoad;
    if (busTbody && isBusTableEmpty) busTbody.innerHTML = htmlBus;
}


let lastRawMsg = '';

function logAlarm(msg) {
    if (!msg) return;

    // Ekstrak pesan murni tanpa timestamp untuk deduplikasi
    const bracketIndex = msg.indexOf(']');
    const rawMsg = bracketIndex !== -1 ? msg.substring(bracketIndex + 2) : msg;

    // Jangan append jika pesannya sama persis (mencegah spam 10x per detik)
    if (rawMsg === lastRawMsg) return;
    lastRawMsg = rawMsg;

    const isError = msg.includes('DEFISIT') || msg.includes('TRIPPED') || msg.includes('UFLS');
    const isSuccess = msg.includes('RESTORASI');
    const isWarning = msg.includes('RE-PRIORITIZED');
    let color = 'inherit';
    if (isError) color = 'var(--danger-red)';
    else if (isSuccess) color = 'var(--success-green)';
    else if (isWarning) color = '#f59e0b'; // amber/orange

    // 1. Update Panel Alarm Kecil (Kiri Bawah)
    const logDiv = document.getElementById('alarm-log-content');
    if (logDiv) {
        const div = document.createElement('div');
        div.style.padding = '8px 0';
        div.style.borderBottom = '1px solid var(--glass-border)';
        div.style.fontFamily = 'var(--font-mono)';
        div.style.fontSize = '0.85rem';
        div.style.color = color;
        div.innerText = msg;

        logDiv.prepend(div);
        if (logDiv.children.length > 50) logDiv.lastChild.remove();
    }

    // 2. Update Tab History Log Utama (HANYA KETIKA ADA GANGGUAN / EVENT PENTING)
    // Filter out pesan rutin seperti "Sistem Stabil." atau "Menunggu..."
    const isRoutineMessage = rawMsg.includes('Sistem Stabil') || rawMsg.includes('Menunggu');

    if (!isRoutineMessage) {
        const histDiv = document.getElementById('history-log-content');
        if (histDiv) {
            const hdiv = document.createElement('div');
            hdiv.style.padding = '12px 0';
            hdiv.style.borderBottom = '1px dashed rgba(255,255,255,0.1)';
            hdiv.style.color = color;

            const timestamp = bracketIndex !== -1 ? msg.substring(0, bracketIndex + 1) : '';
            hdiv.innerHTML = `<span style="color:var(--text-muted); margin-right:15px;">${timestamp}</span> <strong>${rawMsg}</strong>`;

            histDiv.prepend(hdiv);
            // Simpan memori lebih panjang (500 log) untuk tab history
            if (histDiv.children.length > 500) histDiv.lastChild.remove();
        }
    }
}

// --- CONTINGENCY MATRIX ---
function updateContingency(data) {
    const tbody = document.getElementById('contingency-body');
    if (!tbody || !data.contingency || !data.loads) return;

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
const LOAD_NAMES = ['L101', 'L201', 'L303', 'L304', 'L305', 'L401', 'L301', 'L302', 'L402', 'L403', 'L404', 'L405'];
const MAX_LOADS = [20, 20, 15, 20, 30, 30, 5, 10, 5, 10, 15, 20];

function initOverrides() {
    const role = new URLSearchParams(window.location.search).get('role') || 'viewer';
    document.getElementById('role-badge').innerText = role.toUpperCase();

    if (role === 'admin') {
        document.querySelectorAll('.admin-only').forEach(el => el.style.display = '');
    } else {
        document.querySelectorAll('.admin-only').forEach(el => el.style.display = 'none');
    }

    const container = document.getElementById('override-inputs');
    if (!container) return;

    let html = '';
    LOAD_NAMES.forEach((name, i) => {
        html += `
            <div style="display:flex; justify-content:space-between; align-items:center;
                        background: rgba(0,0,0,0.2); padding: 8px 12px; border-radius: 8px;
                        border: 1px solid var(--glass-border);">
                <span style="font-family:var(--font-mono); font-size:0.9rem; font-weight:bold;
                             width:50px; color:var(--text-main);">${name}</span>

                <div style="display:flex; gap:10px; align-items:center;">
                    <!-- Toggle Switch AUTO ↔ MANUAL -->
                    <label class="toggle-wrapper" id="toggle-wrap-${i}">
                        <span class="toggle-lbl-auto">AUTO</span>
                        <input type="checkbox" class="toggle-input" id="mode-${i}"
                               onchange="onToggleMode(${i})">
                        <span class="toggle-track"></span>
                        <span class="toggle-lbl-manual">MANUAL</span>
                    </label>

                    <!-- MW input — aktif hanya saat MANUAL -->
                    <input type="number" id="ov-${i}" value="0"
                           min="0" max="${MAX_LOADS[i]}" disabled
                           style="width:52px; background:rgba(0,0,0,0.3);
                                  border:1px solid var(--glass-border); border-radius:5px;
                                  color:var(--text-muted); font-family:var(--font-mono);
                                  font-size:0.85rem; text-align:right; padding:4px 6px;
                                  transition:all 0.3s; outline:none;">
                    <span style="font-size:0.8rem; color:var(--text-muted); width:45px;">
                        / ${MAX_LOADS[i]} MW
                    </span>
                </div>
            </div>`;
    });
    container.innerHTML = html;
}

// Toggle handler — dipanggil saat checkbox berubah
window.onToggleMode = function (i) {
    const checkbox = document.getElementById(`mode-${i}`);
    const wrapper = document.getElementById(`toggle-wrap-${i}`);
    const input = document.getElementById(`ov-${i}`);
    const isManual = checkbox.checked;

    if (isManual) {
        wrapper.classList.add('is-manual');
        input.disabled = false;
        input.style.color = 'var(--text-main)';
        input.style.borderColor = '#4285f4';
        input.style.boxShadow = '0 0 0 2px rgba(66,133,244,0.2)';
        if (parseInt(input.value) === 0) input.value = MAX_LOADS[i];
    } else {
        wrapper.classList.remove('is-manual');
        input.disabled = true;
        input.style.color = 'var(--text-muted)';
        input.style.borderColor = 'var(--glass-border)';
        input.style.boxShadow = 'none';
    }
};

// Alias lama untuk keamanan
window.toggleMode = window.onToggleMode;

function sendOverrides() {
    if (!socket) return;
    const vals = [];
    for (let i = 0; i < 12; i++) {
        const checkbox = document.getElementById(`mode-${i}`);
        const input = document.getElementById(`ov-${i}`);
        const isManual = checkbox && checkbox.checked;
        vals.push(isManual ? (parseInt(input.value) || 0) : 0);
    }
    socket.emit('set_load_interrupt', { loads: vals });
    alert('✅ Sensor Override Configuration sent to PLC!');
}

window.sendSinglePriority = function (loadId, rawVal) {
    if (!socket) return;
    const prio = parseInt(rawVal) || 2;
    socket.emit('set_load_priority', { load: loadId, priority: prio });
    console.log(`Auto-update Priority: ${loadId} → ${prio}`);
};

window.sendAllPriorities = function () {
    if (!socket) return;
    const inputs = document.querySelectorAll('.priority-input');
    inputs.forEach(input => {
        const id = input.id.replace('prio-input-', '');
        const prio = parseInt(input.value) || 2;
        socket.emit('set_load_priority', { load: id, priority: prio });
    });
    alert('✅ All Priorities Updated!');
};

// --- CONTROLS ---
function toggleGen(name, action) {
    if (!socket) return;
    socket.emit('gen_control', { name, action });
}

