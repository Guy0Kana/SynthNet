#!/usr/bin/env python3

"""
SynthNet Web Dashboard
Real-time visualization of SDN QoS traffic statistics
"""

from flask import Flask, render_template, jsonify, send_from_directory
from flask_cors import CORS
import pandas as pd
import os
import json
from datetime import datetime
import time

# Get the directory where this file is located
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(
    __name__,
    template_folder=BASE_DIR,
    static_folder=BASE_DIR
)
CORS(app)

LOG_FILE = os.path.join(BASE_DIR, "..", "mininet", "logs", "traffic_gen.csv")

# Traffic type configuration - includes all possible profile names
TRAFFIC_TYPES = {
    # Base types
    'voip': {'color': '#00e676', 'priority': 10, 'label': 'VoIP'},
    'cloud': {'color': '#2979ff', 'priority': 9, 'label': 'Cloud/Email'},
    'cloud_email': {'color': '#2979ff', 'priority': 9, 'label': 'Cloud/Email'},
    'dns': {'color': '#00bcd4', 'priority': 8, 'label': 'DNS'},
    'http': {'color': '#ffeb3b', 'priority': 5, 'label': 'HTTP/Web'},
    'http_burst': {'color': '#ffeb3b', 'priority': 5, 'label': 'HTTP'},
    'http_burst0': {'color': '#ffeb3b', 'priority': 5, 'label': 'HTTP'},
    'http_burst1': {'color': '#ffeb3b', 'priority': 5, 'label': 'HTTP'},
    'http_burst2': {'color': '#ffeb3b', 'priority': 5, 'label': 'HTTP'},
    'http_burst3': {'color': '#ffeb3b', 'priority': 5, 'label': 'HTTP'},
    'video': {'color': '#ff5722', 'priority': 2, 'label': 'Video'},
    'ftp': {'color': '#ff9800', 'priority': 2, 'label': 'FTP'},
    'background': {'color': '#78909c', 'priority': 1, 'label': 'Background'},
    'background_chunk': {'color': '#78909c', 'priority': 1, 'label': 'Background'},
    'background_chunk0': {'color': '#78909c', 'priority': 1, 'label': 'Background'},
    'background_chunk1': {'color': '#78909c', 'priority': 1, 'label': 'Background'},
    'background_chunk2': {'color': '#78909c', 'priority': 1, 'label': 'Background'},
    'p2p': {'color': '#e91e63', 'priority': 1, 'label': 'P2P'},
    'ping': {'color': '#4dd0e1', 'priority': 8, 'label': 'Ping/ICMP'},
}

PRIORITY_ORDER = sorted(
    [(k, v['priority']) for k, v in TRAFFIC_TYPES.items() if k in ['voip', 'cloud_email', 'dns', 'http', 'video', 'ftp', 'background', 'p2p']],
    key=lambda x: x[1],
    reverse=True
)

def normalize_profile(profile):
    """Normalize profile names for grouping"""
    if not profile:
        return 'unknown'
    if profile.startswith('http_burst'):
        return 'http'
    if profile.startswith('background_chunk'):
        return 'background'
    if profile == 'cloud':
        return 'cloud_email'
    if profile == 'ping':
        return 'dns'
    return profile

@app.route('/')
def index():
    """Render dashboard"""
    return render_template('index.html')

@app.route('/style.css')
def serve_css():
    """Serve CSS file"""
    return send_from_directory(BASE_DIR, 'style.css')

@app.route('/dashboard.js')
def serve_js():
    """Serve JavaScript file"""
    return send_from_directory(BASE_DIR, 'dashboard.js')

@app.route('/api/stats')
def get_stats():
    """Get traffic statistics"""
    try:
        if os.path.exists(LOG_FILE):
            df = pd.read_csv(LOG_FILE)
            
            if len(df) == 0:
                return jsonify({
                    'success': True,
                    'latest': [],
                    'totals': {},
                    'total_flows': 0,
                    'last_update': None,
                    'traffic_types': TRAFFIC_TYPES,
                    'priority_order': PRIORITY_ORDER
                })
            
            # Normalize profiles for grouping
            df['normalized_profile'] = df['profile'].apply(normalize_profile)
            
            # Get latest entries
            latest = df.tail(20).to_dict('records')
            
            # Calculate totals by normalized profile
            totals = df.groupby('normalized_profile')['mbps'].sum().to_dict()
            
            # Also get raw profile totals for display
            raw_totals = df.groupby('profile')['mbps'].sum().to_dict()
            
            # Get latest timestamp
            latest_time = df['timestamp'].max() if not df.empty else None
            
            return jsonify({
                'success': True,
                'latest': latest,
                'totals': totals,
                'raw_totals': raw_totals,
                'total_flows': len(df),
                'last_update': latest_time,
                'traffic_types': TRAFFIC_TYPES,
                'priority_order': PRIORITY_ORDER
            })
        else:
            return jsonify({
                'success': True,
                'latest': [],
                'totals': {},
                'total_flows': 0,
                'last_update': None,
                'traffic_types': TRAFFIC_TYPES,
                'priority_order': PRIORITY_ORDER
            })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/flows')
def get_flows():
    """Get installed OpenFlow flows"""
    sample_flows = [
        {'priority': 10, 'type': 'voip', 'src': '10.0.0.1', 'dst': '10.0.0.10', 'action': 'NORMAL'},
        {'priority': 5, 'type': 'http', 'src': '10.0.0.3', 'dst': '10.0.0.10', 'action': 'NORMAL'},
        {'priority': 2, 'type': 'video', 'src': '10.0.0.2', 'dst': '10.0.0.10', 'action': 'NORMAL'},
    ]
    return jsonify({
        'success': True,
        'flows': sample_flows
    })

@app.route('/api/clear')
def clear_stats():
    """Clear statistics"""
    if os.path.exists(LOG_FILE):
        backup = f"{LOG_FILE}.bak"
        os.rename(LOG_FILE, backup)
    return jsonify({'success': True})

@app.route('/api/export')
def export_stats():
    """Export stats as JSON"""
    if os.path.exists(LOG_FILE):
        df = pd.read_csv(LOG_FILE)
        data = df.to_dict('records')
        return jsonify(data)
    return jsonify([])

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)
