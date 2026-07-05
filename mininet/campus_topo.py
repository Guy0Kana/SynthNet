#!/usr/bin/env python3

from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel

import os
import time

# ============================================================
# TC SERVER CONFIGURATION
# ============================================================

# Host → (tc_server_port, interface)
TC_SERVERS = {
    'h1': (9001, 'h1-eth0'),
    'h2': (9002, 'h2-eth0'),
    'h3': (9003, 'h3-eth0'),
    'h4': (9004, 'h4-eth0'),
    'h5': (9005, 'h5-eth0'),
    'h6': (9006, 'h6-eth0'),
}

# Path to tiny_tc_server.py — adjust if needed
TC_SERVER_SCRIPT = os.path.expanduser('~/SynthNet/ryu/tiny_tc_server.py')

# ============================================================
# TOPOLOGY
# ============================================================

class CampusTopo(Topo):
    def build(self):

        s1 = self.addSwitch("s1")  # OpenFlow switch
        server = self.addHost("server", ip="10.0.0.10/24")  # Server as traffic destination

        # Client hosts
        h1 = self.addHost("h1", ip="10.0.0.1/24")  # browsing
        h2 = self.addHost("h2", ip="10.0.0.2/24")  # video conferencing
        h3 = self.addHost("h3", ip="10.0.0.3/24")  # VoIP
        h4 = self.addHost("h4", ip="10.0.0.4/24")  # file transfer
        h5 = self.addHost("h5", ip="10.0.0.5/24")  # P2P
        h6 = self.addHost("h6", ip="10.0.0.6/24")  # cloud/email

        link_opts = dict(bw=1000, delay="5ms", loss=0)  # 1Gbps, 5ms delay, no loss

        for host in [server, h1, h2, h3, h4, h5, h6]:
            self.addLink(host, s1, cls=TCLink, **link_opts)


# ============================================================
# TC SERVER FUNCTIONS
# ============================================================

def start_tc_servers(net):
    """
    Start tiny_tc_server.py on each host after net.start().
    Each server listens on its own port and manages its host's interface.
    """
    print("\n*** Starting tc servers on all hosts...")
    
    # Check if tc server script exists
    if not os.path.exists(TC_SERVER_SCRIPT):
        print(f"⚠️  WARNING: tc server script not found at {TC_SERVER_SCRIPT}")
        print("   TC bandwidth control will be disabled!")
        return False

    for host_name, (port, intf) in TC_SERVERS.items():
        host = net.get(host_name)

        # Kill any existing instance
        host.cmd(f"pkill -f 'tiny_tc_server.py {port}' 2>/dev/null")

        # Start in background
        host.cmd(
            f"python3 {TC_SERVER_SCRIPT} {port} {intf} "
            f"> /tmp/tc_{host_name}.log 2>&1 &"
        )
        print(f"    {host_name}: starting tc server on port {port} (interface {intf})")

    # Give servers time to start
    time.sleep(2)

    # Verify all servers are up
    print("\n*** Verifying tc servers...")
    all_ok = True
    for host_name, (port, intf) in TC_SERVERS.items():
        host = net.get(host_name)
        ip = host.IP()
        try:
            result = host.cmd(
                f"curl -sf http://{ip}:{port}/health --max-time 2"
            )
            if 'ok' in result:
                print(f"    ✅ {host_name}: tc server OK (port {port})")
            else:
                print(f"    ❌ {host_name}: tc server FAILED — check /tmp/tc_{host_name}.log")
                all_ok = False
        except Exception as e:
            print(f"    ❌ {host_name}: health check error — {e}")
            all_ok = False

    if all_ok:
        print("*** All tc servers running.\n")
    else:
        print("*** WARNING: Some tc servers failed to start.\n")

    return all_ok


def stop_tc_servers(net):
    """Stop all tc servers cleanly."""
    print("\n*** Stopping tc servers...")
    for host_name in TC_SERVERS:
        host = net.get(host_name)
        host.cmd("pkill -f tiny_tc_server.py 2>/dev/null")
    print("*** tc servers stopped.")


# ============================================================
# MAIN RUN FUNCTION
# ============================================================

def run():
    topo = CampusTopo()  # create topology
    net = Mininet(
        topo=topo,
        controller=RemoteController(
            "ryu",
            ip="127.0.0.1",  # Runs on same machine
            port=6633  # Default OpenFlow port
        ),
        link=TCLink,
        autoSetMacs=True  # Automatically assign MAC addresses
    )

    net.start()

    # Enable OpenFlow 1.3 and Meters
    s1 = net.get('s1')
    s1.cmd('ovs-vsctl set bridge s1 protocols=OpenFlow13')
    s1.cmd('ovs-vsctl set bridge s1 other_config:meter-max=255')
    print("✅ OpenFlow 1.3 and meters enabled on switch")

    # ── START TC SERVERS ──
    tc_ok = start_tc_servers(net)

    print("\n" + "=" * 50)
    print("Network Started Successfully!")
    print("=" * 50)
    print(f"Hosts: {[h.name for h in net.hosts]}")
    print(f"Server IP: 10.0.0.10")
    print(f"Controller: {net.controllers[0].name} @ {net.controllers[0].ip}:{net.controllers[0].port}")
    print(f"TC Servers: {'✅ Running' if tc_ok else '❌ Disabled'}")
    print("\nAvailable hosts:")
    print("  h1 - Web browsing (tc: 9001)")
    print("  h2 - Video conferencing (tc: 9002)")
    print("  h3 - VoIP (tc: 9003)")
    print("  h4 - File transfer (tc: 9004)")
    print("  h5 - P2P (tc: 9005)")
    print("  h6 - Cloud/Email (tc: 9006)")
    print("  server - Destination")
    print("=" * 50 + "\n")

    # Start multiple iperf3 servers on different ports
    print("Starting iperf3 servers on ports 5201-5206...")
    for port in range(5201, 5207):
        net.get('server').cmd(f"iperf3 -s -p {port} &")
    print("All servers ready.")

    # Load iperf3 traffic script if exists
    if os.path.exists('iperf3_traffic.py'):
        with open('iperf3_traffic.py') as f:
            exec(f.read(), {'net': net, 'SERVER_HOST': 'server', **globals()})
    else:
        print("⚠️  iperf3_traffic.py not found — skipping automatic traffic generation")

    # Open Mininet CLI for interactive commands
    CLI(net)

    # ── STOP TC SERVERS ON EXIT ──
    stop_tc_servers(net)

    # Stop the network when CLI exits
    net.stop()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    setLogLevel("info")
    run()
