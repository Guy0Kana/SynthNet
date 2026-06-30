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
import csv
from datetime import datetime
import time
import traceback

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
        # Check if file exists
        if not os.path.exists(LOG_FILE):
            return jsonify({
                'success': True,
                'latest': [],
                'totals': {},
                'total_flows': 0,
                'last_update': None,
                'traffic_types': TRAFFIC_TYPES,
                'priority_order': PRIORITY_ORDER
            })
        
        # Try reading CSV with different methods
        df = None
        
        # Method 1: Try pandas with error handling
        try:
            df = pd.read_csv(LOG_FILE, on_bad_lines='skip')
            if df.empty:
                df = None
        except Exception as e:
            print(f"Pandas read error: {e}")
            df = None
        
        # Method 2: Try manual CSV reading if pandas fails
        if df is None or df.empty:
            try:
                data = []
                with open(LOG_FILE, 'r') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        # Convert numeric values
                        for key in ['mbps', 'bytes', 'retransmits', 'lost_packets']:
                            if key in row and row[key]:
                                try:
                                    row[key] = float(row[key])
                                except:
                                    row[key] = 0
                        data.append(row)
                
                if data:
                    df = pd.DataFrame(data)
            except Exception as e:
                print(f"CSV manual read error: {e}")
                df = None
        
        # If still no data, return empty
        if df is None or df.empty:
            return jsonify({
                'success': True,
                'latest': [],
                'totals': {},
                'total_flows': 0,
                'last_update': None,
                'traffic_types': TRAFFIC_TYPES,
                'priority_order': PRIORITY_ORDER
            })
        
        # Clean data - replace NaN/None with 0
        df = df.fillna(0)
        
        # Ensure mbps is numeric
        if 'mbps' in df.columns:
            df['mbps'] = pd.to_numeric(df['mbps'], errors='coerce').fillna(0)
        
        # Normalize profiles for grouping
        if 'profile' in df.columns:
            df['normalized_profile'] = df['profile'].apply(normalize_profile)
        else:
            df['normalized_profile'] = 'unknown'
        
        # Get latest entries (last 20)
        latest = df.tail(20).to_dict('records')
        
        # Clean latest for JSON
        for row in latest:
            for key in ['mbps', 'bytes', 'retransmits', 'lost_packets']:
                if key in row:
                    if pd.isna(row[key]) or row[key] is None:
                        row[key] = 0
                    try:
                        row[key] = float(row[key])
                    except:
                        row[key] = 0
        
        # Calculate totals by normalized profile
        totals = df.groupby('normalized_profile')['mbps'].sum().to_dict()
        
        # Clean totals
        totals = {k: float(v) if not pd.isna(v) else 0 for k, v in totals.items()}
        
        # Get latest timestamp
        latest_time = df['timestamp'].max() if 'timestamp' in df.columns and not df.empty else None
        
        return jsonify({
            'success': True,
            'latest': latest,
            'totals': totals,
            'total_flows': len(df),
            'last_update': latest_time,
            'traffic_types': TRAFFIC_TYPES,
            'priority_order': PRIORITY_ORDER
        })
        
    except Exception as e:
        print(f"Error in /api/stats: {e}")
        traceback.print_exc()
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
        try:
            df = pd.read_csv(LOG_FILE)
            data = df.to_dict('records')
            return jsonify(data)
        except:
            return jsonify([])
    return jsonify([])

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)
