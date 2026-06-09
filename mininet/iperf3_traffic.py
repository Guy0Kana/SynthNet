#!/usr/bin/env python3

#Run within mininet CLI

import csv, json, threading
from time import sleep
from datetime import datetime

SERVER_HOST = "server"
LOG_FILE    = "logs/traffic_gen.csv"

_results = []
_lock    = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _server_ip():
    return net.get(SERVER_HOST).IP()

def _h(name): #Helper to get host object
    return net.get(name)


def _log(profile, host, protocol, bw_requested, raw_json):
    """Parse iperf3 output and append one row to _results."""
    try:
        # Try to find complete JSON in the output
        import re
        json_match = re.search(r'\{.*\}', raw_json, re.DOTALL)
        if not json_match:
            print(f"  [LOG] {profile} on {host.name}: No JSON found in output")
            return

        data = json.loads(json_match.group())

        # Check for iperf3 error
        if 'error' in data:
            print(f"  [LOG] {profile} on {host.name}: iperf3 error - check server is running")
            return

        # Extract metrics based on available data
        mbps = 0
        jitter_ms = 'n/a'
        lost_pct = 'n/a'

        # Try different possible JSON structures
        if 'end' in data:
            if 'sum_sent' in data['end']:
                mbps = round(data['end']['sum_sent'].get('bits_per_second', 0) / 1e6, 2)
            elif 'streams' in data['end'] and len(data['end']['streams']) > 0:
                sender = data['end']['streams'][0].get('sender', {})
                mbps = round(sender.get('bits_per_second', 0) / 1e6, 2)
                udp = data['end']['streams'][0].get('udp', {})
                jitter_ms = udp.get('jitter_ms', 'n/a')
                lost_pct = udp.get('lost_percent', 'n/a')

        row = {
            "timestamp": datetime.now().isoformat(),
            "profile": profile,
            "host": host.name,
            "host_ip": host.IP(),
            "protocol": protocol,
            "bw_requested": bw_requested,
            "mbps": mbps,
            "bytes": data.get('end', {}).get('sum_sent', {}).get('bytes', 0),
            "retransmits": data.get('end', {}).get('sum_sent', {}).get('retransmits', 'n/a'),
            "jitter_ms": jitter_ms,
            "lost_packets": data.get('end', {}).get('sum_sent', {}).get('lost_packets', 0),
            "lost_pct": lost_pct,
            "flow_priority": "",
            "qos_class": "",
        }
        with _lock:
            _results.append(row)
        print(f"  [LOG] {profile} on {host.name}: {mbps} Mbps")
    except (json.JSONDecodeError, KeyError, IndexError, ValueError) as e:
        # Don't print errors for now — traffic still works
        pass

def save_logs():
    """Write collected results to CSV."""
    if not _results:
        print("No results to save yet.")
        return
    import os; os.makedirs("logs", exist_ok=True)
    with open(LOG_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_results[0].keys())
        writer.writeheader()
        writer.writerows(_results)
    print(f"Saved {len(_results)} rows -> {LOG_FILE}")


def _run_concurrent(fns):
    """Run a list of zero-argument callables concurrently and wait for all."""
    threads = [threading.Thread(target=fn) for fn in fns]
    for t in threads: t.start()
    for t in threads: t.join()


# ---------------------------------------------------------------------------
# Individual traffic functions
# ---------------------------------------------------------------------------

def run_voip(host, duration=30):
    """VoIP (G.711) — 64 Kbps UDP, 128-byte packets"""
    print(f"[VoIP]          {host.name} -> server:5201  ({duration}s)")
    out = host.cmd(f"iperf3 -c {_server_ip()} -p 5201  -u -b 64k -l 128 -t {duration} --json")
    _log("voip", host, "udp", "64k", out)

def run_video(host, duration=30):
    """HD Video stream — 2 Mbps UDP, 1400-byte packets"""
    print(f"[Video]         {host.name} -> server:5202  ({duration}s)")
    out = host.cmd(f"iperf3 -c {_server_ip()} -p 5202  -u -b 5M -l 1400 -t {duration} --json")
    _log("video", host, "udp", "5M", out)

def run_web(host, duration=30):
    """Web browsing — 4 parallel TCP streams"""
    print(f"[Web]           {host.name} -> server:5203  ({duration}s)")
    out = host.cmd(f"iperf3 -c {_server_ip()} -p 5203  -P 4 -t {duration} --json")
    _log("http", host, "tcp", "unlimited", out)

def run_file_transfer(host, duration=30):
    """FTP / bulk transfer — 100 Mbps TCP"""
    print(f"[File Transfer] {host.name} -> server:5204  ({duration}s)")
    out = host.cmd(f"iperf3 -c {_server_ip()} -p 5204  -b 100M -t {duration} --json")
    _log("ftp", host, "tcp", "100M", out)

def run_background(host, duration=30):
    """Background best-effort — 5 Mbps TCP"""
    print(f"[Background]    {host.name} -> server:5205  ({duration}s)")
    out = host.cmd(f"iperf3 -c {_server_ip()} -p 5205  -b 5M -t {duration} --json")
    _log("background", host, "tcp", "5M", out)

def run_dns(host, count=20):
    """DNS simulation — repeated dig queries (fire and forget)"""
    print(f"[DNS]           {host.name}  ({count} queries)")
    host.cmd(f"for i in $(seq 1 {count}); do dig @8.8.8.8 example.com +short > /dev/null 2>&1; sleep 0.1; done &")

def run_ping(host, count=10):
    """ICMP latency baseline"""
    print(f"[Ping]          {host.name} -> {SERVER_HOST}")
    result = host.cmd(f"ping -c {count} {_server_ip()}")

def run_cloud(host, duration=30):
    """Cloud/Email simulation — mixed TCP traffic"""
    print(f"[Cloud]         {host.name} -> server:5206  ({duration}s)")
    out = host.cmd(f"iperf3 -c {_server_ip()} -p 5206  -b 10M -t {duration} --json")
    _log("cloud", host, "tcp", "10M", out)

    row = {
        "timestamp": datetime.now().isoformat(),
        "profile": "ping",
        "host": host.name,
        "host_ip": host.IP(),
        "protocol": "icmp",
        "bw_requested": "",
        "mbps": "0",
        "bytes": "0",
        "retransmits": "0",
        "jitter_ms": "0",
        "lost_packets": "0",
        "lost_pct": "0",
        "flow_priority": "",
        "qos_class": "",
    }

    with _lock:
        _results.append(row)
        
    return out


# ---------------------------------------------------------------------------
# Composite runners
# ---------------------------------------------------------------------------

def run_all_traffic(duration=30):
    """Run all traffic types concurrently from dedicated hosts."""
    print("\n" + "="*50)
    print(f"  ALL Traffic  |  {duration}s  |  server={SERVER_HOST} ({_server_ip()})")
    print("="*50 + "\n")

    h1 = _h('h1')
    h2 = _h('h2')
    h3 = _h('h3')
    h4 = _h('h4')
    h5 = _h('h5')
    h6 = _h('h6')

    _run_concurrent([
        lambda: run_voip(h1, duration=duration),
        lambda: run_video(h2, duration=duration),
        lambda: run_web(h3, duration=duration),
        lambda: run_file_transfer(h4, duration=duration),
        lambda: run_background(h5, duration=duration),
        lambda: run_cloud(h6, duration=duration), 
    ])


    print("\n  All flows done. Call save_logs() to export results.")
    print("="*50)


def run_voip_vs_web(duration=60):
    """Priority test — VoIP (h1) vs Web (h3) competing for bandwidth."""
    print("\n" + "="*50)
    print("  TEST: VoIP (high priority) vs Web (low priority)")
    print("="*50 + "\n")

    h1 = _h('h1')
    h3 = _h('h3')

    _run_concurrent([
        lambda: run_voip(h1, duration=duration),
        lambda: run_web(h3, duration=duration),
    ])

    print("\n  Done. Check results with save_logs().")


def run_stress_test(duration=60, streams=5):
    """High load — 5 parallel TCP streams from each of h1-h4 concurrently."""
    print("\n" + "="*50)
    print(f"  STRESS TEST  |  {streams} streams x 4 hosts  |  {duration}s")
    print("="*50 + "\n")

    def _stress(host, name):
        print(f"[Stress]        {name}  ({streams} parallel TCP streams)")
        out = host.cmd(f"iperf3 -c {_server_ip()} -P {streams} -t {duration} --json")
        _log("stress", host, "tcp", f"{streams}xTCP", out)

    hosts = [('h1', _h('h1')), ('h2', _h('h2')), ('h3', _h('h3')), ('h4', _h('h4'))]

    _run_concurrent([
        lambda name=name, host = host: _stress(host, name)
        for name, host in hosts
    ])

    print("\n  Stress test done. Call save_logs() to export results.")
    print("="*50)


def stop_all_traffic():
    """Kill all iperf3 processes on every host."""
    print("Stopping all iperf3 processes...")
    for host in net.hosts:
        host.cmd("pkill iperf3")
    print("Done.")


# ---------------------------------------------------------------------------
# Ready prompt
# ---------------------------------------------------------------------------

print("\n Traffic generator loaded!")
print(f"  Server: {SERVER_HOST} ({_server_ip()})")
print("\nComposite commands:")
print("  run_all_traffic()          - All types concurrently")
print("  run_voip_vs_web()          - Priority test: VoIP vs Web")
print("  run_stress_test()          - High load: 5xTCP per host")
print("  stop_all_traffic()         - Kill all iperf3 processes")
print("  save_logs()                - Export results to CSV")
print("\nIndividual commands:")
print("  run_voip(h1)               - VoIP on h1")
print("  run_video(h2)              - Video on h2")
print("  run_web(h3)                - Web on h3")
print("  run_file_transfer(h4)      - File transfer on h4")
print("  run_background(h5)         - Background on h5")
print("  run_dns(h5)                - DNS simulation on h5")
print("  run_ping(h1)               - Ping latency test")
