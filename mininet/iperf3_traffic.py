#!/usr/bin/env python3

# Run within mininet CLI
#
# Design goals vs. the original script:
#   1. Every profile differs from every other profile in MORE than just
#      bandwidth cap - protocol, packet size, flow count, and duty cycle
#      (continuous vs. bursty) all vary, so a classifier has real features
#      to separate on instead of everything collapsing toward one
#      "steady single TCP stream" shape.
#   2. VoIP (10M) and Cloud (50M) get protected bandwidth via Linux `tc` 
#      prioritization (HTB + prio bands) applied to each host's outgoing veth.
#   3. Web, FTP, and Background are throttled under QoS.
#   4. Two demo scenarios: NO QoS (chaos) vs WITH QoS (protection).

import csv, json, threading, re, os, time
from time import sleep
from datetime import datetime

SERVER_HOST = "server"
LOG_FILE    = "logs/traffic_gen.csv"

# Link capacity assumption for QoS shaping - adjust to match your topo's
# link bandwidth (Mbit). Used to size the priority bands below.
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
# Server Management
# ---------------------------------------------------------------------------

def start_all_servers():
    """Start iperf3 servers on all ports"""
    print("Starting iperf3 servers on ports 5201-5206...")
    server = net.get('server')
    # Kill any existing servers first
    server.cmd("pkill -9 iperf3")
    sleep(1)
    
    for port in range(5201, 5207):
        server.cmd(f"iperf3 -s -p {port} -D")
        print(f"  Started server on port {port}")
        sleep(0.1)
    
    # Verify
    result = server.cmd("ps aux | grep iperf3 | grep -v grep | wc -l")
    count = int(result.strip())
    print(f"✅ {count} iperf3 servers running")
    
    # Show running servers
    print("\nRunning servers:")
    print(server.cmd("ps aux | grep iperf3 | grep -v grep"))

def stop_all_servers():
    """Stop all iperf3 servers"""
    print("Stopping all iperf3 servers...")
    server = net.get('server')
    server.cmd("pkill -9 iperf3")
    print("✅ All iperf3 servers stopped")


# ---------------------------------------------------------------------------
# QoS setup - protects VoIP (10M) and Cloud (50M)
# ---------------------------------------------------------------------------
#
# Applies an HTB root with priority bands on each host's primary
# outgoing interface:
#   Band 0 (prio 0, 10M guaranteed + can burst): VoIP   - port 5201
#   Band 1 (prio 1, 50M guaranteed + can burst): Cloud  - port 5206
#   Band 2 (prio 2, 5M guaranteed): Video              - port 5202
#   Band 3 (prio 3, throttled): Web (5203) + FTP (5204)
#   Band 4 (prio 4, heavily throttled): Background (5205)

QOS_PORTS = {
    "voip":  5201,
    "video": 5202,
    "web":   5203,
    "ftp":   5204,
    "background": 5205,
    "cloud": 5206,
}

def setup_qos():
    """QoS with VoIP=10M, Cloud=50M guaranteed, others throttled"""
    print("Applying QoS to protect VoIP (10M) and Cloud (50M)...")
    print("  VoIP: 10M guaranteed (prio 0)")
    print("  Cloud: 50M guaranteed (prio 1)")
    print("  Video: 5M guaranteed (prio 2)")
    print("  Web/FTP: throttled to 10M (prio 3)")
    print("  Background: throttled to 2M (prio 4)")
    
    for host in net.hosts:
        if host.name == SERVER_HOST:
            continue
        intf = host.defaultIntf().name
        
        # Clean existing rules
        host.cmd(f"tc qdisc del dev {intf} root 2>/dev/null")
        
        # Root with LINK_MBIT total
        host.cmd(f"tc qdisc add dev {intf} root handle 1: htb default 30")
        host.cmd(f"tc class add dev {intf} parent 1: classid 1:1 htb rate {LINK_MBIT}mbit")
        
        # VoIP: 10M guaranteed, can burst to 15M
        host.cmd(f"tc class add dev {intf} parent 1:1 classid 1:10 htb "
                  f"rate 10mbit ceil 15mbit prio 0")
        
        # Cloud: 50M guaranteed, can burst to 60M
        host.cmd(f"tc class add dev {intf} parent 1:1 classid 1:20 htb "
                  f"rate 50mbit ceil 60mbit prio 1")
        
        # Video: 5M guaranteed, can burst to 10M
        host.cmd(f"tc class add dev {intf} parent 1:1 classid 1:25 htb "
                  f"rate 5mbit ceil 10mbit prio 2")
        
        # Best-Effort (Web, FTP): Throttled to 10M
        host.cmd(f"tc class add dev {intf} parent 1:1 classid 1:30 htb "
                  f"rate 10mbit ceil 15mbit prio 3")
        
        # Background: Heavily throttled to 2M
        host.cmd(f"tc class add dev {intf} parent 1:1 classid 1:40 htb "
                  f"rate 2mbit ceil 5mbit prio 4")
        
        # Filters to match traffic by destination port
        host.cmd(f"tc filter add dev {intf} parent 1: protocol ip prio 1 u32 "
                  f"match ip dport {QOS_PORTS['voip']} 0xffff flowid 1:10")
        
        host.cmd(f"tc filter add dev {intf} parent 1: protocol ip prio 2 u32 "
                  f"match ip dport {QOS_PORTS['cloud']} 0xffff flowid 1:20")
        
        host.cmd(f"tc filter add dev {intf} parent 1: protocol ip prio 3 u32 "
                  f"match ip dport {QOS_PORTS['video']} 0xffff flowid 1:25")
        
        host.cmd(f"tc filter add dev {intf} parent 1: protocol ip prio 4 u32 "
                  f"match ip dport {QOS_PORTS['web']} 0xffff flowid 1:30")
        
        host.cmd(f"tc filter add dev {intf} parent 1: protocol ip prio 5 u32 "
                  f"match ip dport {QOS_PORTS['ftp']} 0xffff flowid 1:30")
        
        host.cmd(f"tc filter add dev {intf} parent 1: protocol ip prio 6 u32 "
                  f"match ip dport {QOS_PORTS['background']} 0xffff flowid 1:40")
    
    print("✅ QoS applied successfully!")


def clear_qos():
    """Remove all QoS rules"""
    print("Clearing QoS rules...")
    for host in net.hosts:
        if host.name == SERVER_HOST:
            continue
        intf = host.defaultIntf().name
        host.cmd(f"tc qdisc del dev {intf} root 2>/dev/null")
    print("✅ QoS cleared.")


# ---------------------------------------------------------------------------
# Individual traffic functions
# ---------------------------------------------------------------------------

def run_voip(host, duration=30):
    """VoIP - 10 Mbps UDP (your requirement)"""
    print(f"[VoIP]          {host.name} -> server:5201  (10M, {duration}s)")
    out = host.cmd(f"iperf3 -c {_server_ip()} -p 5201 -u -b 10M -l 1400 -t {duration} --json")
    _log("voip", host, "udp", "10M", out, flow_priority="high", qos_class="EF")


def run_video(host, duration=30):
    """HD video stream - 5 Mbps UDP, large packets, continuous"""
    print(f"[Video]         {host.name} -> server:5202  (5M, {duration}s)")
    out = host.cmd(f"iperf3 -c {_server_ip()} -p 5202 -u -b 5M -l 1400 -t {duration} --json")
    _log("video", host, "udp", "5M", out, flow_priority="medium", qos_class="AF41")


def run_web(host, duration=30):
    """Web browsing - bursty: repeated short TCP bursts with idle gaps"""
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
    """FTP / bulk transfer - single TCP stream, uncapped"""
    print(f"[File Transfer] {host.name} -> server:5204  ({duration}s)")
    out = host.cmd(f"iperf3 -c {_server_ip()} -p 5204 -t {duration} --json")
    _log("ftp", host, "tcp", "unlimited", out, flow_priority="low", qos_class="bulk")


def run_background(host, duration=30):
    """Background best-effort - low duty-cycle TCP"""
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
    """Cloud/Email sync - 50 Mbps TCP, protected by QoS"""
    print(f"[Cloud]         {host.name} -> server:5206  (50M, {duration}s)")
    out = host.cmd(f"iperf3 -c {_server_ip()} -p 5206 -b 50M -t {duration} --json")
    _log("cloud", host, "tcp", "50M", out, flow_priority="high", qos_class="AF31")


def run_dns(host, count=20):
    """DNS simulation - many tiny, very-short-lived request bursts"""
    print(f"[DNS]           {host.name}  ({count} queries)")
    start = datetime.now()
    out = host.cmd(
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


# ---------------------------------------------------------------------------
# Demo Functions
# ---------------------------------------------------------------------------

def run_demo_no_qos(duration=30):
    """Scenario 1: All traffic WITHOUT QoS (chaos)"""
    print("\n" + "="*60)
    print("  SCENARIO 1: NO QoS - All traffic competes equally")
    print("  Expected: VoIP and Cloud get starved by web/FTP")
    print("="*60 + "\n")
    
    # Clear any existing QoS
    clear_qos()
    
    # Run all traffic
    _run_concurrent([
        lambda: run_voip(net.get('h1'), duration),
        lambda: run_video(net.get('h2'), duration),
        lambda: run_web(net.get('h3'), duration),
        lambda: run_file_transfer(net.get('h4'), duration),
        lambda: run_background(net.get('h5'), duration),
        lambda: run_cloud(net.get('h6'), duration),
    ])
    
    print("\n  ✅ Scenario 1 complete!")
    print("  Check logs/traffic_gen.csv - Look for VoIP/Cloud getting low throughput")
    print("="*60)


def run_demo_with_qos(duration=30):
    """Scenario 2: All traffic WITH QoS (protection)"""
    print("\n" + "="*60)
    print("  SCENARIO 2: WITH QoS - VoIP and Cloud protected")
    print("  Expected: VoIP=10M, Cloud=50M, others throttled")
    print("="*60 + "\n")
    
    # Apply QoS
    setup_qos()
    
    # Run all traffic
    _run_concurrent([
        lambda: run_voip(net.get('h1'), duration),
        lambda: run_video(net.get('h2'), duration),
        lambda: run_web(net.get('h3'), duration),
        lambda: run_file_transfer(net.get('h4'), duration),
        lambda: run_background(net.get('h5'), duration),
        lambda: run_cloud(net.get('h6'), duration),
    ])
    
    print("\n  ✅ Scenario 2 complete!")
    print("  Check logs/traffic_gen.csv - Look for VoIP=10M, Cloud=50M exactly!")
    print("="*60)


def complete_demo(duration=20):
    """Run both scenarios and compare results"""
    print("\n" + "="*60)
    print("  COMPLETE DEMO: QoS Protection Comparison")
    print(f"  Duration: {duration}s per scenario")
    print("="*60)
    
    # Start servers
    start_all_servers()
    time.sleep(2)
    
    # Scenario 1: No QoS
    print("\n" + "="*60)
    print("  📊 SCENARIO 1: NO QoS (Chaos)")
    print("="*60)
    run_demo_no_qos(duration)
    time.sleep(2)
    
    # Save results
    save_logs()
    print("\n📊 Scenario 1 results saved!")
    
    # Clear logs for scenario 2
    global _results
    _results = []
    
    # Scenario 2: With QoS
    print("\n" + "="*60)
    print("  📊 SCENARIO 2: WITH QoS (Protection)")
    print("="*60)
    run_demo_with_qos(duration)
    time.sleep(2)
    
    # Save results
    save_logs()
    print("\n📊 Scenario 2 results saved!")
    
    print("\n" + "="*60)
    print("  ✅ DEMO COMPLETE!")
    print("  Compare results:")
    print("   - Without QoS: VoIP/Cloud should be starved")
    print("   - With QoS: VoIP=10M, Cloud=50M, others throttled")
    print("="*60)


def run_all_traffic(duration=30, with_qos=True):
    """Run all traffic types concurrently from dedicated hosts."""
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


def run_stress_test(duration=60, streams=5, with_qos=True):
    """High load - 5 parallel TCP streams from each of h1-h4 concurrently"""
    print("\n" + "="*50)
    print(f"  STRESS TEST  |  {streams} streams x 4 hosts  |  {duration}s")
    print("="*50 + "\n")

    if with_qos:
        setup_qos()

    def _stress(host, name, port):
        print(f"[Stress]        {name}  ({streams} parallel TCP streams) -> server:{port}")
        out = host.cmd(f"iperf3 -c {_server_ip()} -p {port} -P {streams} -t {duration} --json")
        _log("stress", host, "tcp", f"{streams}xTCP", out,
             flow_priority="low", qos_class="best-effort")

    # Assign different ports to avoid conflicts
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
    """Kill all iperf3 processes on every host."""
    print("Stopping all iperf3 processes...")
    for host in net.hosts:
        host.cmd("pkill iperf3")
    print("Done.")


# ---------------------------------------------------------------------------
# Ready prompt
# ---------------------------------------------------------------------------

print("\n" + "="*60)
print("  TRAFFIC GENERATOR LOADED!")
print("="*60)
print(f"  Server: {SERVER_HOST} ({_server_ip()})")
print("\n📊 DEMO FUNCTIONS:")
print("  complete_demo(duration=20)  - Run both scenarios (NO QoS vs WITH QoS)")
print("  run_demo_no_qos(duration)   - Scenario 1: Chaos (no protection)")
print("  run_demo_with_qos(duration) - Scenario 2: QoS protection")
print("\n🔧 QoS MANAGEMENT:")
print("  setup_qos()  - Apply QoS (VoIP=10M, Cloud=50M)")
print("  clear_qos()  - Remove all QoS rules")
print("\n📡 SERVER MANAGEMENT:")
print("  start_all_servers()  - Start iperf3 servers on ports 5201-5206")
print("  stop_all_servers()   - Stop all iperf3 servers")
print("\n🚦 INDIVIDUAL TRAFFIC:")
print("  run_voip(h1)  - 10M UDP (protected)")
print("  run_cloud(h6) - 50M TCP (protected)")
print("  run_video(h2) - 5M UDP")
print("  run_web(h3)   - Bursty TCP (throttled under QoS)")
print("  run_file_transfer(h4) - Bulk TCP (throttled under QoS)")
print("  run_background(h5) - Low duty-cycle (heavily throttled)")
print("\n📊 OTHER:")
print("  run_all_traffic(duration)  - All types with QoS")
print("  run_stress_test(duration)  - TCP stress test")
print("  save_logs()                - Export results to CSV")
print("="*60)
