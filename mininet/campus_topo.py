#!/usr/bin/env python3

from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel

class CampusTopo(Topo):
    def build(self):

        s1 = self.addSwitch("s1") #OpenFLow switch
        server = self.addHost("server", ip = "10.0.0.10/24") #Server as traffic destination

        #Client hosts
        h1 = self.addHost("h1", ip = "10.0.0.1/24") #browsing
        h2 = self.addHost("h2", ip = "10.0.0.2/24") #video conferencing
        h3 = self.addHost("h3", ip = "10.0.0.3/24") #VoIP
        h4 = self.addHost("h4", ip = "10.0.0.4/24") #file transfer
        h5 = self.addHost("h5", ip = "10.0.0.5/24") #P2P
        h6 = self.addHost("h6", ip = "10.0.0.6/24") #cloud/email

        link_opts = dict(bw = 1000, delay = "5ms", loss = 0) #1Gbps, 5ms delay, no loss. Can increase loss to simulate congestion/test robustness

        for host in [server, h1, h2, h3, h4, h5, h6]:
            self.addLink(host, s1, cls = TCLink, **link_opts)


def run():
    topo = CampusTopo() #create topology
    net = Mininet(
        topo = topo,
        controller = RemoteController(
            "ryu",
            ip = "127.0.0.1", #Runs on same machine
            port = 6633 #Default OpenFLow port
        ),
        link = TCLink,
        autoSetMacs = True #Automatically assign MAC addresses
    )

    net.start()

    #Enable OpenFLow 1.3 and Meters
    s1 = net.get('s1')
    s1.cmd('ovs-vsctl set bridge s1 protocols=OpenFlow13')
    s1.cmd('ovs-vsctl set bridge s1 other_config:meter-max=255')
    print("✅ OpenFlow 1.3 and meters enabled on switch")

    print("\n" + "="*50)
    print("Network Started Successfully!")
    print("="*50)
    print(f"Hosts: {[h.name for h in net.hosts]}")
    print(f"Server IP: 10.0.0.10")
    print(f"Controller: {net.controllers[0].name} @ {net.controllers[0].ip}:{net.controllers[0].port}")
    print("\nAvailable hosts:")
    print("  h1 - Web browsing")
    print("  h2 - Video conferencing")
    print("  h3 - VoIP")
    print("  h4 - File transfer")
    print("  h5 - P2P")
    print("  h6 - Cloud/Email")
    print("  server - Destination")
    print("="*50 + "\n")

    with open('iperf3_traffic.py') as f:
        exec(f.read(), {'net': net, 'SERVER_HOST': 'server', **globals()})

    # Start multiple iperf3 servers on different ports
    print("Starting iperf3 servers on ports 5201-5206...")
    for port in range(5201, 5207):
        net.get('server').cmd(f"iperf3 -s -p {port} &")
    print("All servers ready.")

    # Open Mininet CLI for interactive commands
    CLI(net)

    # Stop the network when CLI exits
    net.stop()


if __name__ == "__main__":
    setLogLevel("info")
    run()
