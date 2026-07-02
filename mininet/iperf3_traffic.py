#!/usr/bin/env python3

"""
SynthNet Traffic Generator - Continuous Traffic Support
All profiles run continuously with different characteristics.
"""

import csv, json, threading, re, os
from time import sleep
from datetime import datetime

SERVER_HOST = "server"
LOG_FILE    = "logs/traffic_gen.csv"

# Link capacity assumption for QoS shaping
LINK_MBIT = 1000  # 1 Gbps link

_results = []
_lock    = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _server_ip():
    return net.get(SERVER_HOST).IP()

def _h(name):
    return net.get(name)


def _append_row(profile, host, protocol, bw_requested, mbps=0, nbytes=0,
                 retransmits='n/a', jitter_ms='n/a', lost_packets=0,
                 lost_pct='n/a', flow_priority='', qos_class=''):
    row = {
        "timestamp": datetime.now().isoformat(),
        "profile": profile,
        "host": host.name,
        "host_ip": host.IP(),
        "protocol": protocol,
        "bw_requested": bw_requested,
        "mbps": mbps,
        "bytes": nbytes,
        "retransmits": retransmits,
        "jitter_ms": jitter_ms,
        "lost_packets": lost_packets,
        "lost_pct": lost_pct,
        "flow_priority": flow_priority,
        "qos_class": qos_class,
    }
    with _lock:
        _results.append(row)
    return row


def _log(profile, host, protocol, bw_requested, raw_json, flow_priority='', qos_class=''):
    """Parse iperf3 JSON output and append one row to _results."""
    try:
        json_match = re.search(r'\{.*\}', raw_json, re.DOTALL)
        if not json_match:
            print(f"  [LOG] {profile} on {host.name}: No JSON found")
            return
        data = json.loads(json_match.group())

        if 'error' in data:
            print(f"  [LOG] {profile} on {host.name}: iperf3 error - server running?")
            return

        mbps = 0
        jitter_ms = 'n/a'
        lost_pct = 'n/a'
        lost_packets = 0

        end = data.get('end', {})
        if 'sum_sent' in end:
            mbps = round(end['sum_sent'].get('bits_per_second', 0) / 1e6, 2)
        elif 'streams' in end and len(end['streams']) > 0:
            sender = end['streams'][0].get('sender', {})
            mbps = round(sender.get('bits_per_second', 0) / 1e6, 2)
            udp = end['streams'][0].get('udp', {})
            jitter_ms = udp.get('jitter_ms', 'n/a')
            lost_pct = udp.get('lost_percent', 'n/a')

        _append_row(
            profile, host, protocol, bw_requested,
            mbps=mbps,
            nbytes=end.get('sum_sent', {}).get('bytes', 0),
            retransmits=end.get('sum_sent', {}).get('retransmits', 'n/a'),
            jitter_ms=jitter_ms,
            lost_packets=end.get('sum_sent', {}).get('lost_packets', 0),
            lost_pct=lost_pct,
            flow_priority=flow_priority,
            qos_class=qos_class,
        )
        print(f"  [LOG] {profile} on {host.name}: {mbps} Mbps")
    except (json.JSONDecodeError, KeyError, IndexError, ValueError) as e:
        print(f"  [LOG] {profile} on {host.name}: parse error - {e}")


def save_logs():
    if not _results:
        print("No results to save yet.")
        return
    os.makedirs("logs", exist_ok=True)
    with open(LOG_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_results[0].keys())
        writer.writeheader()
        writer.writerows(_results)
    print(f"Saved {len(_results)} rows -> {LOG_FILE}")


def _run_concurrent(fns):
    threads = [threading.Thread(target=fn) for fn in fns]
    for t in threads: t.start()
    for t in threads: t.join()


# ---------------------------------------------------------------------------
# QoS setup - protects VoIP and Cloud bandwidth
# ---------------------------------------------------------------------------

QOS_PORTS = {
    "voip":  5201,
    "cloud": 5206,
}

def setup_qos():
    print("Applying QoS (tc/HTB) to protect VoIP and Cloud bandwidth...")
    for host in net.hosts:
        if host.name == SERVER_HOST:
            continue
        intf = host.defaultIntf().name
        host.cmd(f"tc qdisc del dev {intf} root 2>/dev/null")
        host.cmd(f"tc qdisc add dev {intf} root handle 1: htb default 30")
        host.cmd(f"tc class add dev {intf} parent 1: classid 1:1 htb rate {LINK_MBIT}mbit")
        
        # VoIP: 10% guaranteed (100 Mbps), highest priority
        host.cmd(f"tc class add dev {intf} parent 1:1 classid 1:10 htb "
                  f"rate {max(10, int(LINK_MBIT*0.10))}mbit ceil {LINK_MBIT}mbit prio 0")
        # Cloud: 20% guaranteed (200 Mbps), high priority
        host.cmd(f"tc class add dev {intf} parent 1:1 classid 1:20 htb "
                  f"rate {max(20, int(LINK_MBIT*0.20))}mbit ceil {LINK_MBIT}mbit prio 1")
        # Everything else: best effort
        host.cmd(f"tc class add dev {intf} parent 1:1 classid 1:30 htb "
                  f"rate {max(10, int(LINK_MBIT*0.15))}mbit ceil {LINK_MBIT}mbit prio 2")

        host.cmd(f"tc filter add dev {intf} parent 1: protocol ip prio 1 u32 "
                  f"match ip dport {QOS_PORTS['voip']} 0xffff flowid 1:10")
        host.cmd(f"tc filter add dev {intf} parent 1: protocol ip prio 2 u32 "
                  f"match ip dport {QOS_PORTS['cloud']} 0xffff flowid 1:20")
    print("QoS applied: VoIP=prio0 (10% guaranteed), Cloud=prio1 (20% guaranteed)")


def clear_qos():
    print("Clearing QoS rules...")
    for host in net.hosts:
        if host.name == SERVER_HOST:
            continue
        intf = host.defaultIntf().name
        host.cmd(f"tc qdisc del dev {intf} root 2>/dev/null")
    print("Done.")


# ---------------------------------------------------------------------------
# Individual traffic functions - CONTINUOUS TRAFFIC
# ---------------------------------------------------------------------------

def run_voip(host, duration=60):
    """VoIP - Continuous UDP, 64 Kbps, 128-byte packets"""
    print(f"[VoIP]          {host.name} -> server:5201  ({duration}s, continuous)")
    out = host.cmd(f"iperf3 -c {_server_ip()} -p 5201 -u -b 64k -l 128 -t {duration} --json")
    _log("voip", host, "udp", "64k", out, flow_priority="high", qos_class="EF")


def run_video(host, duration=60):
    """Video - Continuous UDP, 5 Mbps, 1400-byte packets"""
    print(f"[Video]         {host.name} -> server:5202  ({duration}s, continuous)")
    out = host.cmd(f"iperf3 -c {_server_ip()} -p 5202 -u -b 5M -l 1400 -t {duration} --json")
    _log("video", host, "udp", "5M", out, flow_priority="medium", qos_class="AF41")


def run_web(host, duration=60):
    """Web - Continuous TCP with parallel streams (simulates many connections)"""
    print(f"[Web]           {host.name} -> server:5203  ({duration}s, continuous, 4 streams)")
    out = host.cmd(f"iperf3 -c {_server_ip()} -p 5203 -P 4 -t {duration} --json")
    _log("http_continuous", host, "tcp", "unlimited", out,
         flow_priority="low", qos_class="best-effort")


def run_file_transfer(host, duration=60):
    """FTP - Continuous high-throughput TCP"""
    print(f"[File Transfer] {host.name} -> server:5204  ({duration}s, continuous)")
    out = host.cmd(f"iperf3 -c {_server_ip()} -p 5204 -b 200M -t {duration} --json")
    _log("ftp", host, "tcp", "200M", out, flow_priority="low", qos_class="bulk")


def run_background(host, duration=60):
    """Background - Continuous low-rate TCP"""
    print(f"[Background]    {host.name} -> server:5205  ({duration}s, continuous)")
    out = host.cmd(f"iperf3 -c {_server_ip()} -p 5205 -b 5M -t {duration} --json")
    _log("background", host, "tcp", "5M", out,
         flow_priority="lowest", qos_class="background")


def run_cloud(host, duration=60):
    """Cloud - Continuous TCP, protected, high bandwidth"""
    print(f"[Cloud]         {host.name} -> server:5206  ({duration}s, continuous)")
    out = host.cmd(f"iperf3 -c {_server_ip()} -p 5206 -b 50M -t {duration} --json")
    _log("cloud", host, "tcp", "50M", out, flow_priority="high", qos_class="AF31")


# ---------------------------------------------------------------------------
# Continuous monitoring functions
# ---------------------------------------------------------------------------

def run_continuous_traffic(duration=60, with_qos=True):
    """Run all traffic types continuously for a specified duration"""
    print("\n" + "="*60)
    print(f"  CONTINUOUS TRAFFIC  |  {duration}s  |  server={SERVER_HOST} ({_server_ip()})")
    print("  All flows run continuously for the entire duration")
    print("="*60 + "\n")

    if with_qos:
        setup_qos()

    h1, h2, h3, h4, h5, h6 = (_h(f'h{i}') for i in range(1, 7))

    _run_concurrent([
        lambda: run_voip(h1, duration=duration),
        lambda: run_video(h2, duration=duration),
        lambda: run_web(h3, duration=duration),
        lambda: run_file_transfer(h4, duration=duration),
        lambda: run_background(h5, duration=duration),
        lambda: run_cloud(h6, duration=duration),
    ])

    print("\n  All continuous flows done. Call save_logs() to export results.")
    print("="*60)


def run_continuous_stress(duration=60, streams=8, with_qos=True):
    """High load stress test with continuous parallel streams"""
    print("\n" + "="*60)
    print(f"  STRESS TEST  |  {streams} streams x 4 hosts  |  {duration}s")
    print("  Continuous high-load traffic")
    print("="*60 + "\n")

    if with_qos:
        setup_qos()

    def _stress(host, name, port):
        print(f"[Stress]        {name}  ({streams} streams) -> port {port}")
        out = host.cmd(f"iperf3 -c {_server_ip()} -p {port} -P {streams} -t {duration} --json")
        _log("stress", host, "tcp", f"{streams}xTCP", out,
             flow_priority="low", qos_class="best-effort")

    hosts = [
        ('h1', _h('h1'), 5201),
        ('h2', _h('h2'), 5202),
        ('h3', _h('h3'), 5203),
        ('h4', _h('h4'), 5204),
    ]

    _run_concurrent([
        lambda name=name, host=host, port=port: _stress(host, name, port)
        for name, host, port in hosts
    ])

    print("\n  Stress test done. Call save_logs() to export results.")
    print("="*60)


# ---------------------------------------------------------------------------
# Ready prompt
# ---------------------------------------------------------------------------

print("\n✅ Traffic generator loaded!")
print(f"  Server: {SERVER_HOST} ({_server_ip()})")
print("\n📊 QoS Commands:")
print("  setup_qos()                 - Protect VoIP/Cloud bandwidth")
print("  clear_qos()                 - Remove tc rules")
print("\n🚀 Continuous Traffic Commands:")
print("  run_continuous_traffic(60)  - All 6 flows continuous for 60s")
print("  run_continuous_stress(60)   - High load continuous stress test")
print("  run_all_traffic(30)         - Standard mixed traffic (bursty)")
print("  run_voip_vs_web(30)         - VoIP vs Web priority test")
print("  stop_all_traffic()          - Kill all iperf3 processes")
print("  save_logs()                 - Export results to CSV")
