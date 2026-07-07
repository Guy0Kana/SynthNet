#!/usr/bin/env python3

# Run within mininet CLI

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
            print(f"  [LOG] {profile} on {host.name}: No JSON found in output")
            return
        data = json.loads(json_match.group())

        if 'error' in data:
            print(f"  [LOG] {profile} on {host.name}: iperf3 error - check server is running")
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
# QoS setup - protects VoIP and Cloud only (Video is now best-effort)
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
        # Clear existing QoS first
        host.cmd(f"tc qdisc del dev {intf} root 2>/dev/null")
        host.cmd(f"tc qdisc add dev {intf} root handle 1: htb default 30")
        host.cmd(f"tc class add dev {intf} parent 1: classid 1:1 htb rate {LINK_MBIT}mbit")
        
        # VoIP: 20% guaranteed (highest priority)
        host.cmd(f"tc class add dev {intf} parent 1:1 classid 1:10 htb "
                  f"rate {max(10, int(LINK_MBIT*0.20))}mbit ceil {LINK_MBIT}mbit prio 0")
        # Cloud: 30% guaranteed (high priority) — increased to 30M
        host.cmd(f"tc class add dev {intf} parent 1:1 classid 1:20 htb "
                  f"rate {max(30, int(LINK_MBIT*0.30))}mbit ceil {LINK_MBIT}mbit prio 1")
        # Everything else (Video, HTTP, FTP, Background): best effort
        host.cmd(f"tc class add dev {intf} parent 1:1 classid 1:30 htb "
                  f"rate {max(5, int(LINK_MBIT*0.05))}mbit ceil {LINK_MBIT}mbit prio 2")

        host.cmd(f"tc filter add dev {intf} parent 1: protocol ip prio 1 u32 "
                  f"match ip dport {QOS_PORTS['voip']} 0xffff flowid 1:10")
        host.cmd(f"tc filter add dev {intf} parent 1: protocol ip prio 2 u32 "
                  f"match ip dport {QOS_PORTS['cloud']} 0xffff flowid 1:20")
    print("QoS applied: VoIP=20% (200M), Cloud=30% (300M), rest=best-effort (50M)")


def clear_qos():
    print("Clearing QoS rules...")
    for host in net.hosts:
        if host.name == SERVER_HOST:
            continue
        intf = host.defaultIntf().name
        host.cmd(f"tc qdisc del dev {intf} root 2>/dev/null")
    print("Done.")


# ---------------------------------------------------------------------------
# Individual traffic functions
# ---------------------------------------------------------------------------

def run_voip(host, duration=30):
    """VoIP - 10 Mbps UDP (high priority, protected)"""
    print(f"[VoIP]          {host.name} -> server:5201  ({duration}s)")
    out = host.cmd(f"iperf3 -c {_server_ip()} -p 5201 -u -b 10M -l 1400 -t {duration} --json")
    _log("voip", host, "udp", "10M", out, flow_priority="high", qos_class="EF")


def run_video(host, duration=30):
    """Video - 20 Mbps UDP (best-effort, NOT protected anymore)"""
    print(f"[Video]         {host.name} -> server:5202  ({duration}s)")
    out = host.cmd(f"iperf3 -c {_server_ip()} -p 5202 -u -b 20M -l 1400 -t {duration} --json")
    _log("video", host, "udp", "20M", out, flow_priority="medium", qos_class="best-effort")


def run_web(host, duration=30):
    """Web browsing - bursty TCP with idle gaps (severely limited)"""
    print(f"[Web]           {host.name} -> server:5203  ({duration}s, bursty)")
    end_time = duration
    elapsed = 0
    burst_n = 0
    while elapsed < end_time:
        burst_len = min(1, end_time - elapsed)  # Shorter bursts
        out = host.cmd(f"iperf3 -c {_server_ip()} -p 5203 -P 2 -t {burst_len} --json")  # Fewer streams
        _log(f"http_burst{burst_n}", host, "tcp", "unlimited", out,
             flow_priority="low", qos_class="best-effort")
        elapsed += burst_len
        burst_n += 1
        idle = min(2.0, end_time - elapsed)  # Longer idle gaps
        if idle > 0:
            sleep(idle)
            elapsed += idle
    print(f"[Web]           {host.name} done ({burst_n} bursts)")


def run_file_transfer(host, duration=30):
    """FTP / bulk transfer - limited TCP (severely throttled)"""
    print(f"[File Transfer] {host.name} -> server:5204  ({duration}s)")
    out = host.cmd(f"iperf3 -c {_server_ip()} -p 5204 -b 5M -t {duration} --json")  # Limited to 5M
    _log("ftp", host, "tcp", "5M", out, flow_priority="low", qos_class="bulk")


def run_background(host, duration=30):
    """Background - very low duty-cycle TCP (severely starved)"""
    print(f"[Background]    {host.name} -> server:5205  ({duration}s, low duty-cycle)")
    elapsed = 0
    chunk_n = 0
    idle_time = min(8, max(2, duration // 4))  # Longer idle periods
    while elapsed < duration:
        on_time = min(1, duration - elapsed)  # Very short active periods
        out = host.cmd(f"iperf3 -c {_server_ip()} -p 5205 -b 0.5M -t {on_time} --json")  # Only 0.5M
        _log(f"background_chunk{chunk_n}", host, "tcp", "0.5M", out,
             flow_priority="lowest", qos_class="background")
        elapsed += on_time
        chunk_n += 1
        idle = min(idle_time, duration - elapsed)
        if idle > 0:
            sleep(idle)
            elapsed += idle
    print(f"[Background]    {host.name} done ({chunk_n} active chunks)")


def run_cloud(host, duration=30):
    """Cloud/Email - steady TCP, protected, 30 Mbps (increased)"""
    print(f"[Cloud]         {host.name} -> server:5206  ({duration}s)")
    out = host.cmd(f"iperf3 -c {_server_ip()} -p 5206 -b 30M -t {duration} --json")
    _log("cloud", host, "tcp", "30M", out, flow_priority="high", qos_class="AF31")


def run_dns(host, count=20):
    """DNS simulation - many tiny queries"""
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
    """ICMP latency baseline"""
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


def stop_all_traffic():
    """Kill all iperf3 processes on every host."""
    print("Stopping all iperf3 processes...")
    for host in net.hosts:
        host.cmd("pkill -9 iperf3 2>/dev/null")
    print("Done.")


# ---------------------------------------------------------------------------
# Composite runners
# ---------------------------------------------------------------------------

def run_all_traffic(duration=30, with_qos=True):
    """Run all traffic types concurrently from dedicated hosts."""
    # Restart iperf3 servers before each run
    print("🔄 Restarting iperf3 servers...")
    server = net.get(SERVER_HOST)
    server.cmd("pkill -9 iperf3 2>/dev/null; sleep 0.5")
    for port in range(5201, 5207):
        server.cmd(f"iperf3 -s -p {port} -D")
    sleep(0.5)
    
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
    """Priority test - VoIP (h1) vs Web (h3) competing for bandwidth."""
    print("🔄 Restarting iperf3 servers...")
    server = net.get(SERVER_HOST)
    server.cmd("pkill -9 iperf3 2>/dev/null; sleep 0.5")
    for port in range(5201, 5207):
        server.cmd(f"iperf3 -s -p {port} -D")
    sleep(0.5)
    
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


def run_stress_test(duration=30, with_qos=True):
    """
    STRESS TEST: Heavy traffic from ALL hosts (h1-h6)
    Shows QoS protection clearly under heavy load!
    """
    # Restart iperf3 servers
    print("🔄 Restarting iperf3 servers...")
    server = net.get(SERVER_HOST)
    server.cmd("pkill -9 iperf3 2>/dev/null; sleep 0.5")
    for port in range(5201, 5207):
        server.cmd(f"iperf3 -s -p {port} -D")
    sleep(0.5)
    
    print("\n" + "="*70)
    print("  STRESS TEST: Heavy Traffic from ALL Hosts (h1-h6)")
    print("="*70)
    print("  🟢 PROTECTED (should get full bandwidth):")
    print("     h1: VoIP @ 10M  (UDP, Priority 10)")
    print("     h6: Cloud @ 30M  (TCP, Priority 9) — PROTECTED")
    print("  🔴 BEST-EFFORT (should be throttled/starved):")
    print("     h2: Video @ 20M  (UDP, Priority 5) — NOT protected")
    print("     h3: HTTP @ 200M  (TCP, Priority 3) — severely limited")
    print("     h4: FTP @ 5M     (TCP, Priority 2) — severely limited")
    print("     h5: Background @ 0.5M (TCP, Priority 1) — starved")
    print("="*70 + "\n")

    if with_qos:
        setup_qos()
    else:
        clear_qos()
        print("⚠️  QoS DISABLED - All traffic will compete equally!\n")

    server_ip = _server_ip()
    h1, h2, h3, h4, h5, h6 = (_h(f'h{i}') for i in range(1, 7))

    # Function to run and log each flow
    def run_flow(host, port, protocol, bandwidth, profile, priority, qos_class, duration):
        if protocol == "udp":
            out = host.cmd(f"iperf3 -c {server_ip} -p {port} -u -b {bandwidth} -l 1400 -t {duration} --json")
        else:
            out = host.cmd(f"iperf3 -c {server_ip} -p {port} -b {bandwidth} -t {duration} --json")
        _log(profile, host, protocol, bandwidth, out, flow_priority=priority, qos_class=qos_class)

    # Run all flows concurrently
    _run_concurrent([
        # PROTECTED FLOWS (should get full bandwidth)
        lambda: run_flow(h1, 5201, "udp", "10M", "voip", "high", "EF", duration),
        lambda: run_flow(h6, 5206, "tcp", "30M", "cloud", "high", "AF31", duration),
        # BEST-EFFORT FLOWS (should be throttled)
        lambda: run_flow(h2, 5202, "udp", "20M", "video", "medium", "best-effort", duration),
        lambda: run_flow(h3, 5203, "tcp", "200M", "http", "low", "best-effort", duration),
        lambda: run_flow(h4, 5204, "tcp", "5M", "ftp", "low", "bulk", duration),
        lambda: run_flow(h5, 5205, "tcp", "0.5M", "background", "lowest", "background", duration),
    ])

    print("\n" + "="*70)
    print("  STRESS TEST COMPLETE!")
    print("  📊 Expected Results with QoS:")
    print("     ✅ VoIP:  ~10 Mbps  (PROTECTED)")
    print("     ✅ Cloud: ~30 Mbps  (PROTECTED)")
    print("     ❌ Video: ~5-10 Mbps (THROTTLED — not protected)")
    print("     ❌ HTTP:  ~5-15 Mbps (THROTTLED)")
    print("     ❌ FTP:   ~1-3 Mbps  (THROTTLED)")
    print("     ❌ Bkgnd: ~0.1-0.5 Mbps (STARVED)")
    print("="*70)
    print("  Call save_logs() to export results.")


# ============================================================================
# QoS DEMO: Compare With/Without QoS
# ============================================================================

def run_qos_demo(duration=30):
    """Clean QoS demonstration - shows protected vs throttled traffic"""
    run_stress_test(duration, with_qos=True)


def run_no_qos_demo(duration=30):
    """Same traffic WITHOUT QoS - shows what happens without protection"""
    run_stress_test(duration, with_qos=False)


def run_comparison_test(duration=20):
    """Run both tests and compare results"""
    print("\n" + "="*70)
    print("  COMPARISON TEST: QoS vs No-QoS")
    print("  Run 1: No QoS → Run 2: With QoS")
    print("  Compare VoIP and Cloud bandwidth!")
    print("="*70)
    
    # Run No-QoS test
    print("\n📊 TEST 1: NO QoS")
    run_no_qos_demo(duration)
    os.rename("logs/traffic_gen.csv", "logs/traffic_gen_no_qos.csv")
    
    # Clear logs
    _results.clear()
    
    # Run QoS test
    print("\n📊 TEST 2: WITH QoS")
    run_qos_demo(duration)
    os.rename("logs/traffic_gen.csv", "logs/traffic_gen_with_qos.csv")
    
    print("\n" + "="*70)
    print("  COMPARISON COMPLETE!")
    print("  Results saved to:")
    print("    logs/traffic_gen_no_qos.csv")
    print("    logs/traffic_gen_with_qos.csv")
    print("="*70)


# ---------------------------------------------------------------------------
# Ready prompt
# ---------------------------------------------------------------------------

print("\n✅ Traffic generator loaded!")
print(f"  Server: {SERVER_HOST} ({_server_ip()})")
print("\n📊 QoS Commands:")
print("  setup_qos()                 - Protect VoIP/Cloud bandwidth via tc/HTB")
print("  clear_qos()                 - Remove tc rules")
print("\n🚀 Composite commands:")
print("  run_all_traffic(30)         - All 6 traffic types (standard)")
print("  run_stress_test(30)         - Heavy traffic from ALL hosts (h1-h6)")
print("  run_voip_vs_web(60)         - VoIP vs Web priority test")
print("  run_qos_demo(30)            - QoS demo (protected vs throttled)")
print("  run_no_qos_demo(30)         - No-QoS demo (all traffic equal)")
print("  run_comparison_test(20)     - Compare both scenarios")
print("  stop_all_traffic()          - Kill all iperf3 processes")
print("  save_logs()                 - Export results to CSV")
print("\nIndividual commands:")
print("  run_voip(h1)                - VoIP on h1 (10M, protected)")
print("  run_video(h2)               - Video on h2 (20M, best-effort)")
print("  run_web(h3)                 - Web on h3 (bursty, limited)")
print("  run_file_transfer(h4)       - File transfer on h4 (limited to 5M)")
print("  run_background(h5)          - Background on h5 (0.5M, starved)")
print("  run_cloud(h6)               - Cloud on h6 (30M, protected)")
print("  run_dns(h5)                 - DNS simulation on h5")
print("  run_ping(h1)                - Ping latency test")
