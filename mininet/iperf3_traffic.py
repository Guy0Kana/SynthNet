#!/usr/bin/env python3
"""
SynthNet Traffic Generator
Two demo modes:
  run_no_qos_demo(duration) — all traffic competes freely, no shaping
  run_qos_demo(duration)    — voip/cloud get high bandwidth, rest throttled
"""

from time import sleep
from threading import Thread
from datetime import datetime
import json
import os
import subprocess
import sys

try:
    from mininet.net import Mininet
except ImportError:
    print("[WARN] Mininet not available")

# ── Config ────────────────────────────────────────────────────────────────────
SERVER_HOST    = "server"
SERVER_IP      = "10.0.0.10"
LOG_FILE       = "traffic_logs.json"
LINK_MBIT      = 1000
DASHBOARD_PORT = 5000

# Traffic flows: (host, port, label, protocol, rate, priority, qos_class)
FLOWS = [
    ("h1", 5201, "voip",        "udp", "128k",  "highest", "EF"),
    ("h2", 5202, "video",       "tcp", "20M",   "high",    "AF41"),
    ("h3", 5203, "web",         "tcp", "10M",   "medium",  "AF21"),
    ("h4", 5204, "ftp",         "tcp", "30M",   "low",     "AF11"),
    ("h5", 5205, "background",  "tcp", "5M",    "lowest",  "BE"),
    ("h6", 5206, "cloud",       "tcp", "50M",   "highest", "AF31"),
]

# QoS bandwidth allocation per traffic type
# voip and cloud get high guaranteed rates, rest are throttled
QOS_POLICY = {
    "h1": {"rate": 300, "ceil": 1000, "prio": 0},   # voip:       300Mbit guaranteed
    "h2": {"rate":  10, "ceil":  50,  "prio": 3},   # video:       10Mbit throttled
    "h3": {"rate":  10, "ceil":  30,  "prio": 3},   # web:         10Mbit throttled
    "h4": {"rate":  10, "ceil":  30,  "prio": 4},   # ftp:         10Mbit throttled
    "h5": {"rate":   5, "ceil":  10,  "prio": 4},   # background:   5Mbit throttled
    "h6": {"rate": 400, "ceil": 1000, "prio": 0},   # cloud:       400Mbit guaranteed
}

net  = None
_logs = []


# ── Net attachment ────────────────────────────────────────────────────────────

def attach_net(mininet_net):
    global net
    net = mininet_net


def _get_host(name):
    if net is None:
        raise RuntimeError("Call attach_net(net) first.")
    return net.get(name)


def _server_ip():
    return SERVER_IP


# ── Logging ───────────────────────────────────────────────────────────────────

def _log(label, host, protocol, rate, raw_out,
         flow_priority="normal", qos_class="BE", test_type="normal"):
    entry = {
        "timestamp":      datetime.now().isoformat(),
        "label":          label,
        "host":           host.name,
        "protocol":       protocol,
        "rate":           rate,
        "flow_priority":  flow_priority,
        "qos_class":      qos_class,
        "test_type":      test_type,
        "throughput_mbps": None,
        "retransmits":    None,
        "jitter_ms":      None,
        "lost_packets":   None,
        "error":          None,
    }

    try:
        lines      = raw_out.strip().split('\n')
        json_start = next(
            (i for i, l in enumerate(lines) if l.strip().startswith('{')), None
        )
        if json_start is not None:
            data = json.loads('\n'.join(lines[json_start:]))
            end  = data.get("end", {})
            if protocol == "udp":
                udp = end.get("sum", {})
                entry["throughput_mbps"] = round(udp.get("bits_per_second", 0) / 1e6, 3)
                entry["jitter_ms"]       = round(udp.get("jitter_ms", 0), 3)
                entry["lost_packets"]    = udp.get("lost_packets", 0)
            else:
                sent = end.get("sum_sent", {})
                entry["throughput_mbps"] = round(sent.get("bits_per_second", 0) / 1e6, 3)
                entry["retransmits"]     = sent.get("retransmits", 0)
        else:
            entry["error"] = "iperf3 error — check server is running"
    except Exception as e:
        entry["error"] = str(e)

    _logs.append(entry)
    _save_logs_live()

    status = (f"{entry['throughput_mbps']} Mbps"
              if entry['throughput_mbps'] is not None
              else entry['error'])
    print(f"  [LOG] {label} on {host.name}: {status}")


def _save_logs_live():
    with open(LOG_FILE, 'w') as f:
        json.dump(_logs, f, indent=2)


def save_logs():
    _save_logs_live()
    print(f"[LOG] {len(_logs)} entries saved to {LOG_FILE}")


def clear_logs():
    global _logs
    _logs = []
    _save_logs_live()
    print("[LOG] Logs cleared.")


# ── tc helpers ────────────────────────────────────────────────────────────────

def _apply_tc(host, intf, rate_mbit, ceil_mbit, prio):
    """Apply HTB shaping on a host interface."""
    host.cmd(f"tc qdisc del dev {intf} root 2>/dev/null")
    host.cmd(f"tc qdisc add dev {intf} root handle 1: htb default 10")
    host.cmd(f"tc class add dev {intf} parent 1: classid 1:1 htb rate {LINK_MBIT}mbit")
    host.cmd(
        f"tc class add dev {intf} parent 1:1 classid 1:10 "
        f"htb rate {rate_mbit}mbit ceil {ceil_mbit}mbit prio {prio}"
    )
    host.cmd(f"tc qdisc add dev {intf} parent 1:10 handle 10: pfifo limit 1000")
    host.cmd(
        f"tc filter add dev {intf} parent 1: protocol ip prio 1 "
        f"u32 match ip dst 0.0.0.0/0 flowid 1:10"
    )


def _remove_tc(host, intf):
    """Remove all tc rules from a host interface."""
    host.cmd(f"tc qdisc del dev {intf} root 2>/dev/null")


def setup_qos():
    """
    Apply QoS policies:
      - voip (h1):  300Mbit guaranteed, prio 0
      - cloud (h6): 400Mbit guaranteed, prio 0
      - rest:       10-50Mbit throttled, prio 3-4
    """
    print("\n[QoS] Applying bandwidth policies...")
    for host_name, policy in QOS_POLICY.items():
        host = _get_host(host_name)
        intf = f"{host_name}-eth0"
        _apply_tc(
            host, intf,
            policy["rate"],
            policy["ceil"],
            policy["prio"]
        )
        status = (
            "HIGH PRIORITY" if policy["prio"] == 0
            else "throttled"
        )
        print(
            f"  {host_name}: {policy['rate']}Mbit guaranteed, "
            f"ceil {policy['ceil']}Mbit [{status}]"
        )
    print("[QoS] Policies applied.\n")


def teardown_qos():
    """Remove all tc rules — restore full link speed for all hosts."""
    print("\n[QoS] Removing all tc rules...")
    for host_name in QOS_POLICY:
        host = _get_host(host_name)
        intf = f"{host_name}-eth0"
        _remove_tc(host, intf)
        print(f"  {host_name}: tc rules removed")
    print("[QoS] All rules removed.\n")


# ── Server management ─────────────────────────────────────────────────────────

def start_servers():
    """Restart iperf3 servers before each test run."""
    server = _get_host(SERVER_HOST)
    server.cmd("pkill -f iperf3 2>/dev/null; sleep 0.3")
    for port in range(5201, 5207):
        server.cmd(f"iperf3 -s -p {port} -D")
    sleep(0.5)
    print(f"[SERVER] iperf3 listening on ports 5201-5206")


# ── Individual traffic flows ───────────────────────────────────────────────────

def run_voip(host, duration=30, test_type="normal"):
    out = host.cmd(
        f"iperf3 -c {_server_ip()} -p 5201 -u -b 128k -l 160 "
        f"-t {duration} --json"
    )
    _log("voip", host, "udp", "128k", out,
         flow_priority="highest", qos_class="EF", test_type=test_type)


def run_video(host, duration=30, test_type="normal"):
    out = host.cmd(
        f"iperf3 -c {_server_ip()} -p 5202 -b 20M "
        f"-t {duration} --json"
    )
    _log("video", host, "tcp", "20M", out,
         flow_priority="high", qos_class="AF41", test_type=test_type)


def run_web(host, duration=30, test_type="normal"):
    """Bursty HTTP-like traffic."""
    elapsed = 0
    chunk_n = 0
    idle_t  = max(1, duration // 10)
    while elapsed < duration:
        burst = min(2, duration - elapsed)
        if burst <= 0:
            break
        out = host.cmd(
            f"iperf3 -c {_server_ip()} -p 5203 -b 10M "
            f"-t {int(burst)} --json"
        )
        _log(f"web_burst{chunk_n}", host, "tcp", "10M", out,
             flow_priority="medium", qos_class="AF21", test_type=test_type)
        elapsed += burst
        chunk_n += 1
        idle = min(idle_t, duration - elapsed)
        if idle > 0:
            sleep(idle)
            elapsed += idle


def run_ftp(host, duration=30, test_type="normal"):
    out = host.cmd(
        f"iperf3 -c {_server_ip()} -p 5204 -b 30M "
        f"-t {duration} --json"
    )
    _log("ftp", host, "tcp", "30M", out,
         flow_priority="low", qos_class="AF11", test_type=test_type)


def run_background(host, duration=30, test_type="normal"):
    elapsed = 0
    chunk_n = 0
    idle_t  = max(1, duration // 8)
    while elapsed < duration:
        on_time = min(2, duration - elapsed)
        if on_time <= 0:
            break
        out = host.cmd(
            f"iperf3 -c {_server_ip()} -p 5205 -b 1M "
            f"-t {int(on_time)} --json"
        )
        _log(f"background_chunk{chunk_n}", host, "tcp", "1M", out,
             flow_priority="lowest", qos_class="BE", test_type=test_type)
        elapsed += on_time
        chunk_n += 1
        idle = min(idle_t, duration - elapsed)
        if idle > 0:
            sleep(idle)
            elapsed += idle


def run_cloud(host, duration=30, test_type="normal"):
    out = host.cmd(
        f"iperf3 -c {_server_ip()} -p 5206 -b 50M "
        f"-t {duration} --json"
    )
    _log("cloud", host, "tcp", "50M", out,
         flow_priority="highest", qos_class="AF31", test_type=test_type)


# ── Core runner ───────────────────────────────────────────────────────────────

def _run_all(duration, test_type):
    """Run all 6 flows simultaneously."""
    start_servers()

    h1 = _get_host("h1")
    h2 = _get_host("h2")
    h3 = _get_host("h3")
    h4 = _get_host("h4")
    h5 = _get_host("h5")
    h6 = _get_host("h6")

    threads = [
        Thread(target=run_voip,       args=(h1, duration, test_type)),
        Thread(target=run_video,      args=(h2, duration, test_type)),
        Thread(target=run_web,        args=(h3, duration, test_type)),
        Thread(target=run_ftp,        args=(h4, duration, test_type)),
        Thread(target=run_background, args=(h5, duration, test_type)),
        Thread(target=run_cloud,      args=(h6, duration, test_type)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


# ── Demo functions ────────────────────────────────────────────────────────────

def run_no_qos_demo(duration=30):
    """
    No QoS — all hosts compete freely for bandwidth.
    Expect voip/cloud to be starved by greedy ftp/video flows.
    """
    print("\n" + "="*55)
    print(f"  NO-QoS DEMO  |  {duration}s  |  All traffic unrestricted")
    print("="*55)
    print("  All hosts share bandwidth equally — no shaping applied.")
    print("  Expect voip and cloud to be starved.\n")

    # Remove any existing tc rules
    teardown_qos()

    _run_all(duration, test_type="no_qos")

    print("\n" + "="*55)
    print("  No-QoS demo complete. Call save_logs() to export.")
    print("="*55 + "\n")


def run_qos_demo(duration=30):
    """
    QoS active — voip and cloud get guaranteed high bandwidth.
    FTP, video, web, background are throttled.
    Expect voip/cloud to maintain throughput even under congestion.
    """
    print("\n" + "="*55)
    print(f"  QoS DEMO  |  {duration}s  |  voip + cloud prioritised")
    print("="*55)
    print("  voip  (h1): 300Mbit guaranteed")
    print("  cloud (h6): 400Mbit guaranteed")
    print("  video, web, ftp, background: throttled to 5-10Mbit\n")

    # Apply QoS policies before traffic starts
    setup_qos()

    _run_all(duration, test_type="qos")

    print("\n" + "="*55)
    print("  QoS demo complete. Call save_logs() to export.")
    print("="*55 + "\n")


# ── Dashboard ─────────────────────────────────────────────────────────────────

_dashboard_proc = None

def start_dashboard():
    global _dashboard_proc
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "dashboard.py"
    )
    if not os.path.exists(path):
        print(f"[DASHBOARD] dashboard.py not found at {path}")
        return
    _dashboard_proc = subprocess.Popen(
        [sys.executable, path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"[DASHBOARD] http://localhost:{DASHBOARD_PORT}")


def stop_dashboard():
    global _dashboard_proc
    if _dashboard_proc:
        _dashboard_proc.terminate()
        _dashboard_proc = None
        print("[DASHBOARD] Stopped.")


# ── Full demo (both back to back) ─────────────────────────────────────────────

def demo(duration=30):
    """
    Run both demos back to back for a clear before/after comparison.
    Results visible at http://localhost:5000
    """
    print("\n" + "="*55)
    print("  SynthNet Full Demo")
    print("="*55)

    start_dashboard()
    sleep(1.5)

    # Phase 1 — no QoS baseline
    run_no_qos_demo(duration)
    sleep(2)

    # Phase 2 — QoS active
    run_qos_demo(duration)

    # Cleanup
    teardown_qos()
    save_logs()

    print("\n" + "="*55)
    print(f"  Demo complete. View: http://localhost:{DASHBOARD_PORT}")
    print("="*55 + "\n")
