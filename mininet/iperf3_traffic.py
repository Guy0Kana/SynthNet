#!/usr/bin/env python3

# Run within mininet CLI

import csv, json, threading, re, os
from time import sleep
from datetime import datetime

SERVER_HOST = "server"
LOG_FILE    = "logs/traffic_gen.csv"

# Link capacity assumption for QoS shaping
LINK_MBIT = 100

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
            print(f"  [LOG] {profile} on {host.name}: No JSON found in output")
            return
        data = json.loads(json_match.group())

        if 'error' in data:
            print(f"  [LOG] {profile} on {host.name}: iperf3 error - {data['error']}")
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


def clear_results():
    with _lock:
        _results.clear()
    print("In-memory results cleared.")


def _run_concurrent(fns):
    threads = [threading.Thread(target=fn) for fn in fns]
    for t in threads: t.start()
    for t in threads: t.join()


# ---------------------------------------------------------------------------
# QoS setup - dedicated class per traffic type, capped borrowing
# ---------------------------------------------------------------------------
#
# Previous version put video/web/ftp/background all into the htb "default"
# class with ceil=100mbit (same as everyone else). That has two effects:
#   1. VoIP/Cloud only ever send AT their configured target rate (64k / 10M),
#      which is below their guaranteed floor, so they never need to borrow
#      and never contend for leftover bandwidth.
#   2. Whatever's left over goes entirely to the single shared low-priority
#      class, and web's -P 4 parallel TCP streams dominate it, including
#      video, which the comments claimed was protected but never was.
#
# Fix: give every traffic type its own leaf class, and cap ceil on the
# low-priority classes so they can't monopolize 100% of idle capacity even
# when VoIP/Cloud aren't using their reservation.

QOS_PORTS = {
    "voip":  5201,
    "video": 5202,
    "web":   5203,
    "ftp":   5204,
    "background": 5205,
    "cloud": 5206,
}

# (classid, rate_pct, ceil_pct, prio)
QOS_CLASSES = {
    "voip":       ("1:10", 10, 100, 0),  # EF   - guaranteed floor, may burst to full link
    "cloud":      ("1:20", 25, 100, 1),  # AF31 - guaranteed floor, may burst to full link
    "video":      ("1:21", 20,  60, 2),  # AF41 - protected floor, capped borrow
    "web":        ("1:30", 15,  50, 3),  # best-effort, capped so it can't eat everything idle
    "ftp":        ("1:31", 15,  50, 3),  # bulk, same tier as web
    "background": ("1:32",  5,  20, 4),  # lowest priority, small borrow cap
}


def setup_qos():
    print("Applying QoS (tc/HTB): dedicated class per traffic type...")
    for host in net.hosts:
        if host.name == SERVER_HOST:
            continue
        intf = host.defaultIntf().name
        host.cmd(f"tc qdisc del dev {intf} root 2>/dev/null")
        host.cmd(f"tc qdisc add dev {intf} root handle 1: htb default 32")
        host.cmd(f"tc class add dev {intf} parent 1: classid 1:1 htb rate {LINK_MBIT}mbit")

        for name, (classid, rate_pct, ceil_pct, prio) in QOS_CLASSES.items():
            rate = max(1, int(LINK_MBIT * rate_pct / 100))
            ceil = max(rate, int(LINK_MBIT * ceil_pct / 100))
            host.cmd(f"tc class add dev {intf} parent 1:1 classid {classid} htb "
                      f"rate {rate}mbit ceil {ceil}mbit prio {prio}")
            # fq_codel per leaf keeps a single greedy flow (e.g. web's -P 4)
            # from hogging its own class against its siblings in the same bucket.
            host.cmd(f"tc qdisc add dev {intf} parent {classid} handle {classid.split(':')[1]}: fq_codel")

            port = QOS_PORTS[name]
            host.cmd(f"tc filter add dev {intf} parent 1: protocol ip prio {prio+1} u32 "
                      f"match ip dport {port} 0xffff flowid {classid}")

    print("QoS applied — voip/cloud keep guaranteed floors + full-link burst headroom;")
    print("video/web/ftp/background get dedicated classes with capped borrow ceilings.")


def clear_qos():
    print("Clearing QoS rules...")
    for host in net.hosts:
        if host.name == SERVER_HOST:
            continue
        intf = host.defaultIntf().name
        host.cmd(f"tc qdisc del dev {intf} root 2>/dev/null")
    print("Done.")


# ---------------------------------------------------------------------------
# Cross-run cleanup — prevents lag from stacking runs
# ---------------------------------------------------------------------------

def reset_environment(wait=2):
    """Kill stale iperf3 processes (client AND server side) and clear tc
    rules before starting a new test. Call this before every composite run —
    it's what was missing before, and is why a second run would lag/degrade."""
    print("Resetting environment (killing stale iperf3, clearing tc)...")
    for host in net.hosts:
        host.cmd("pkill -9 -f iperf3 2>/dev/null")
    clear_qos()
    sleep(wait)  # let TCP sockets/tc teardown settle before reconfiguring
    print("Environment reset complete.")


# ---------------------------------------------------------------------------
# Individual traffic functions
# ---------------------------------------------------------------------------

def run_voip(host, duration=30):
    print(f"[VoIP]          {host.name} -> server:5201  ({duration}s)")
    out = host.cmd(f"iperf3 -c {_server_ip()} -p 5201 -u -b 64k -l 128 -t {duration} --json")
    _log("voip", host, "udp", "64k", out, flow_priority="high", qos_class="EF")


def run_video(host, duration=30):
    print(f"[Video]         {host.name} -> server:5202  ({duration}s)")
    out = host.cmd(f"iperf3 -c {_server_ip()} -p 5202 -u -b 5M -l 1400 -t {duration} --json")
    _log("video", host, "udp", "5M", out, flow_priority="medium", qos_class="AF41")


def run_web(host, duration=30):
    print(f"[Web]           {host.name} -> server:5203  ({duration}s, bursty)")
    end_time = duration
    elapsed = 0
    burst_n = 0
    while elapsed < end_time:
        burst_len = min(2, end_time - elapsed)
        out = host.cmd(f"iperf3 -c {_server_ip()} -p 5203 -P 4 -t {burst_len} --json")
        _log(f"http_burst{burst_n}", host, "tcp", "unlimited", out,
             flow_priority="low", qos_class="best-effort")
        elapsed += burst_len
        burst_n += 1
        idle = min(1.5, end_time - elapsed)
        if idle > 0:
            sleep(idle)
            elapsed += idle
    print(f"[Web]           {host.name} done ({burst_n} bursts)")


def run_file_transfer(host, duration=30):
    print(f"[File Transfer] {host.name} -> server:5204  ({duration}s)")
    out = host.cmd(f"iperf3 -c {_server_ip()} -p 5204 -t {duration} --json")
    _log("ftp", host, "tcp", "unlimited", out, flow_priority="low", qos_class="bulk")


def run_background(host, duration=30):
    print(f"[Background]    {host.name} -> server:5205  ({duration}s, low duty-cycle)")
    elapsed = 0
    chunk_n = 0
    while elapsed < duration:
        on_time = min(2, duration - elapsed)
        out = host.cmd(f"iperf3 -c {_server_ip()} -p 5205 -b 1M -t {on_time} --json")
        _log(f"background_chunk{chunk_n}", host, "tcp", "1M", out,
             flow_priority="lowest", qos_class="background")
        elapsed += on_time
        chunk_n += 1
        idle = min(6, duration - elapsed)
        if idle > 0:
            sleep(idle)
            elapsed += idle
    print(f"[Background]    {host.name} done ({chunk_n} active chunks)")


def run_cloud(host, duration=30):
    print(f"[Cloud]         {host.name} -> server:5206  ({duration}s)")
    out = host.cmd(f"iperf3 -c {_server_ip()} -p 5206 -b 10M -t {duration} --json")
    _log("cloud", host, "tcp", "10M", out, flow_priority="high", qos_class="AF31")


def run_dns(host, count=20):
    print(f"[DNS]           {host.name}  ({count} queries)")
    start = datetime.now()
    host.cmd(
        f"for i in $(seq 1 {count}); do dig @8.8.8.8 example.com +short > /dev/null 2>&1; sleep 0.1; done"
    )
    elapsed = (datetime.now() - start).total_seconds()
    _append_row(
        "dns", host, "udp", "n/a",
        mbps=0, nbytes=count * 64,
        retransmits='n/a', jitter_ms='n/a', lost_packets=0, lost_pct='n/a',
        flow_priority="medium", qos_class="control",
    )
    print(f"  [LOG] dns on {host.name}: {count} queries in {elapsed:.1f}s")


def run_ping(host, count=10):
    print(f"[Ping]          {host.name} -> {SERVER_HOST}")
    result = host.cmd(f"ping -c {count} {_server_ip()}")
    loss_match = re.search(r'(\d+)% packet loss', result)
    lost_pct = loss_match.group(1) if loss_match else 'n/a'
    _append_row(
        "ping", host, "icmp", "n/a",
        mbps=0, nbytes=0, retransmits='n/a', jitter_ms='n/a',
        lost_packets=0, lost_pct=lost_pct,
        flow_priority="medium", qos_class="control",
    )
    print(f"  [LOG] ping on {host.name}: {lost_pct}% loss")


# ---------------------------------------------------------------------------
# Composite runners
# ---------------------------------------------------------------------------

def run_all_traffic(duration=30, with_qos=True):
    reset_environment()

    print("\n" + "="*50)
    print(f"  ALL Traffic  |  {duration}s  |  server={SERVER_HOST} ({_server_ip()})")
    print("="*50 + "\n")

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

    print("\n  All flows done. Call save_logs() to export results.")
    print("="*50)


def run_voip_vs_web(duration=60, with_qos=True):
    reset_environment()

    print("\n" + "="*50)
    print("  TEST: VoIP (high priority) vs Web (low priority)")
    print("="*50 + "\n")

    if with_qos:
        setup_qos()

    h1, h3 = _h('h1'), _h('h3')

    _run_concurrent([
        lambda: run_voip(h1, duration=duration),
        lambda: run_web(h3, duration=duration),
    ])

    print("\n  Done. Check results with save_logs().")


def run_stress_test(duration=60, streams=5, with_qos=True):
    reset_environment()

    print("\n" + "="*50)
    print(f"  STRESS TEST  |  {streams} streams x 4 hosts  |  {duration}s")
    print("="*50 + "\n")

    if with_qos:
        setup_qos()

    def _stress(host, name, port):
        print(f"[Stress]        {name}  ({streams} parallel TCP streams) -> port {port}")
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
    print("="*50)


def stop_all_traffic():
    print("Stopping all iperf3 processes (client + server)...")
    for host in net.hosts:
        host.cmd("pkill -9 -f iperf3 2>/dev/null")
    print("Done.")


# ---------------------------------------------------------------------------
# Ready prompt
# ---------------------------------------------------------------------------

print("\n✅ Traffic generator loaded!")
print(f"  Server: {SERVER_HOST} ({_server_ip()})")
print("\n📊 QoS Commands:")
print("  setup_qos()                 - Apply per-class QoS (dedicated class per traffic type)")
print("  clear_qos()                 - Remove tc rules")
print("  reset_environment()         - Kill stale iperf3 + clear tc (auto-run before composite tests)")
print("\n🚀 Composite commands:")
print("  run_all_traffic(30)         - All 6 traffic types")
print("  run_voip_vs_web(60)         - VoIP vs Web priority test")
print("  run_stress_test(60)         - High load stress test")
print("  stop_all_traffic()          - Kill all iperf3 processes")
print("  save_logs()                 - Export results to CSV")
print("  clear_results()             - Wipe in-memory results between test batches")
print("\nIndividual commands:")
print("  run_voip(h1)                - VoIP on h1")
print("  run_video(h2)               - Video on h2")
print("  run_web(h3)                 - Web on h3")
print("  run_file_transfer(h4)       - File transfer on h4")
print("  run_background(h5)          - Background on h5")
print("  run_cloud(h6)               - Cloud on h6")
print("  run_dns(h5)                 - DNS simulation on h5")
print("  run_ping(h1)                - Ping latency test")
