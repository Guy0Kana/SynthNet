/**
 * SynthNet QoS Dashboard
 * Real-time traffic monitoring
 */

const API_BASE = '/api';
const REFRESH_INTERVAL = 3000;

// Traffic type colors
const COLORS = {
    'voip': '#00e676',
    'cloud_email': '#2979ff',
    'dns': '#00bcd4',
    'http': '#ffeb3b',
    'video': '#ff5722',
    'ftp': '#ff9800',
    'background': '#78909c',
    'p2p': '#e91e63',
};

function formatBandwidth(mbps) {
    if (mbps >= 1000) return `${(mbps / 1000).toFixed(2)} Gbps`;
    if (mbps >= 1) return `${mbps.toFixed(2)} Mbps`;
    if (mbps >= 0.001) return `${(mbps * 1000).toFixed(0)} Kbps`;
    return `${mbps.toFixed(3)} Mbps`;
}

function formatTime(timestamp) {
    if (!timestamp) return '-';
    try {
        const date = new Date(timestamp);
        return date.toLocaleTimeString();
    } catch {
        return timestamp;
    }
}

function getBadgeClass(profile) {
    const mapping = {
        'voip': 'badge-voip',
        'cloud_email': 'badge-cloud_email',
        'dns': 'badge-dns',
        'http': 'badge-http',
        'video': 'badge-video',
        'ftp': 'badge-ftp',
        'background': 'badge-background',
        'p2p': 'badge-p2p',
    };
    return mapping[profile] || 'badge-background';
}

function updateDashboard(data) {
    if (!data.success) {
        console.error('Error loading data:', data.error);
        return;
    }

    document.getElementById('totalFlows').textContent = data.total_flows || 0;
    
    const activeTypes = Object.keys(data.totals).filter(k => data.totals[k] > 0).length;
    document.getElementById('activeTypes').textContent = activeTypes;
    
    let highest = '-';
    let highestPri = -1;
    for (const [profile, mbps] of Object.entries(data.totals)) {
        if (mbps > 0 && data.traffic_types[profile]) {
            const pri = data.traffic_types[profile].priority;
            if (pri > highestPri) {
                highestPri = pri;
                highest = data.traffic_types[profile].label;
            }
        }
    }
    document.getElementById('highestPriority').textContent = highest;
    document.getElementById('lastUpdateTime').textContent = formatTime(data.last_update);

    updatePriorityGrid(data);
    updateTrafficTable(data.latest);
    updateDistribution(data.totals);
}

function updatePriorityGrid(data) {
    const grid = document.getElementById('priorityGrid');
    if (!grid) return;

    const order = data.priority_order || [];
    let html = '';
    for (const [profile, priority] of order) {
        const info = data.traffic_types[profile];
        if (!info) continue;
        const color = info.color || '#666';
        const active = data.totals[profile] > 0;
        html += `
            <div class="priority-item" style="border-color: ${active ? color : '#1a2636'}; opacity: ${active ? 1 : 0.5}">
                <div class="color-bar" style="background: ${color}"></div>
                <div class="info">
                    <span class="label">${info.label}</span>
                    <span class="priority">Priority ${priority}</span>
                </div>
            </div>
        `;
    }
    grid.innerHTML = html;
}

function updateTrafficTable(entries) {
    const tbody = document.getElementById('tableBody');
    if (!tbody) return;

    if (!entries || entries.length === 0) {
        tbody.innerHTML = `<tr><td colspan="8" class="loading-text">No traffic data available</td></tr>`;
        return;
    }

    const latestEntries = entries.slice().reverse().slice(0, 20);
    let html = '';
    for (const row of latestEntries) {
        const profile = row.profile || 'unknown';
        const badgeClass = getBadgeClass(profile);
        const priority = row.flow_priority || '-';
        
        html += `
            <tr>
                <td>${formatTime(row.timestamp)}</td>
                <td><span class="badge ${badgeClass}">${profile}</span></td>
                <td>${row.host || '-'}</td>
                <td>${row.protocol || '-'}</td>
                <td>${formatBandwidth(row.mbps || 0)}</td>
                <td>${row.jitter_ms !== 'n/a' ? row.jitter_ms + ' ms' : '-'}</td>
                <td>${row.lost_pct !== 'n/a' ? row.lost_pct + '%' : '-'}</td>
                <td>${priority}</td>
            </tr>
        `;
    }
    tbody.innerHTML = html;
}

function updateDistribution(totals) {
    const container = document.getElementById('distributionBars');
    if (!container) return;

    const entries = Object.entries(totals).filter(([_, v]) => v > 0);
    
    if (entries.length === 0) {
        container.innerHTML = '<div style="padding:20px;text-align:center;color:#8892a8;">No traffic data available</div>';
        return;
    }

    const maxVal = Math.max(...entries.map(([_, v]) => v));
    let html = '';
    for (const [profile, mbps] of entries) {
        const color = COLORS[profile] || '#666';
        const percentage = maxVal > 0 ? Math.max((mbps / maxVal) * 100, 5) : 0;
        const label = profile.charAt(0).toUpperCase() + profile.slice(1);
        
        html += `
            <div class="bar-row">
                <span class="bar-label">${label}</span>
                <div class="bar-track">
                    <div class="bar-fill" style="width: ${percentage}%; background: ${color}">
                        ${mbps >= 1 ? mbps.toFixed(1) + ' Mbps' : mbps.toFixed(3) + ' Mbps'}
                    </div>
                </div>
                <span class="bar-value">${formatBandwidth(mbps)}</span>
            </div>
        `;
    }
    container.innerHTML = html;
}

function refreshData() {
    fetch(`${API_BASE}/stats`)
        .then(response => response.json())
        .then(data => {
            updateDashboard(data);
            document.getElementById('lastUpdate').textContent = `Updated: ${new Date().toLocaleTimeString()}`;
        })
        .catch(err => {
            console.error('Failed to fetch data:', err);
        });
}

setInterval(refreshData, REFRESH_INTERVAL);
document.addEventListener('DOMContentLoaded', refreshData);
