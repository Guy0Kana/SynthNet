#!/usr/bin/env python3

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ipv4, tcp, udp
from ryu.lib import hub

import requests
import time
from collections import defaultdict

# ============================================================
# CONFIGURATION
# ============================================================

ML_API_URL = "http://localhost:8000/classify"
FEATURE_TIMEOUT = 60          # Max seconds to collect flow stats
MIN_PACKETS = 5               # Minimum packets needed for stats

# IP of the traffic-gen "server" host. Reverse traffic from it is
# forwarded normally without going through the classifier.
# Must match whatever IP your Mininet topology assigns to the server host.
SERVER_IP = "10.0.0.10"

# QoS Priority Mapping (higher number = higher priority)
# NOTE: this is the map that actually decides installed flow priority.
# Keep it in sync with the FastAPI service's PRIORITY_MAP for consistency,
# even though the API's own priority field isn't used here.
PRIORITY_MAP = {
    'voip': 10,
    'streaming': 7,
    'mail': 6,
    'browsing': 5,
    'chat': 4,
    'ft': 3,
    'p2p': 2,
    'default': 4,
}

# ============================================================
# FLOW BUFFER - Collects 10 features for XGBoost
# ============================================================

class FlowBuffer:
    def __init__(self, flow_key, timeout=FEATURE_TIMEOUT):
        self.flow_key = flow_key
        self.start_time = time.time()
        self.timeout = timeout
        self.completed = False
        self.completed_dispatched = False
        self.origin_src = None
        self.src_ip = None

        # Packet tracking
        self.packet_count = 0
        self.total_bytes = 0

        # IAT tracking
        self.fiat_values = []      # Forward IATs (same direction)
        self.biat_values = []      # Backward IATs (opposite direction)
        self.flowiat_values = []   # All IATs

        # State for IAT calculation
        self.last_timestamp = None
        self.last_direction = None

        # Direction tracking
        self.bytes_fwd = 0
        self.bytes_rev = 0
        self.packets_fwd = 0
        self.packets_rev = 0

    def add_packet(self, pkt_data, timestamp, src_ip):
        """Add a packet and update IAT stats"""
        if not self.origin_src:
            self.origin_src = src_ip
            self.src_ip = src_ip

        # Determine direction
        direction = 1 if src_ip == self.origin_src else -1
        pkt_size = len(pkt_data)

        # Update byte/packet counts
        self.packet_count += 1
        self.total_bytes += pkt_size

        if direction == 1:
            self.bytes_fwd += pkt_size
            self.packets_fwd += 1
        else:
            self.bytes_rev += pkt_size
            self.packets_rev += 1

        # Calculate IATs
        if self.last_timestamp is not None and self.last_direction is not None:
            iat = timestamp - self.last_timestamp

            # Forward IAT: same direction as previous packet
            if self.last_direction == direction:
                self.fiat_values.append(iat)
            # Backward IAT: opposite direction
            else:
                self.biat_values.append(iat)

            # Flow IAT: all packets regardless of direction
            self.flowiat_values.append(iat)

        self.last_timestamp = timestamp
        self.last_direction = direction

        # Check if we have enough data
        if self.packet_count >= MIN_PACKETS:
            self.completed = True
            return True
        return False

    def extract_features(self):
        """Extract the 10 features for XGBoost"""
        duration = time.time() - self.start_time
        if duration < 0.001:
            duration = 0.001

        # Calculate IAT stats
        total_fiat = sum(self.fiat_values) if self.fiat_values else 0
        total_biat = sum(self.biat_values) if self.biat_values else 0
        min_fiat = min(self.fiat_values) if self.fiat_values else 0
        min_biat = min(self.biat_values) if self.biat_values else 0
        max_fiat = max(self.fiat_values) if self.fiat_values else 0
        max_biat = max(self.biat_values) if self.biat_values else 0
        mean_biat = total_biat / len(self.biat_values) if self.biat_values else 0

        flow_iats = self.flowiat_values
        max_flowiat = max(flow_iats) if flow_iats else 0
        mean_flowiat = sum(flow_iats) / len(flow_iats) if flow_iats else 0

        # Return exactly 10 features (matches XGBoost training)
        return [
            duration,       # 1
            total_fiat,     # 2
            total_biat,     # 3
            min_fiat,       # 4
            min_biat,       # 5
            max_fiat,       # 6
            max_biat,       # 7
            mean_biat,      # 8
            max_flowiat,    # 9
            mean_flowiat    # 10
        ]

    def is_expired(self):
        return (time.time() - self.start_time) > self.timeout

    def mark_dispatched(self):
        self.completed_dispatched = True

    def is_dispatched(self):
        return self.completed_dispatched

    # NOTE: previously this was a method named `packet_count`, which
    # collided with the `self.packet_count` int attribute set in
    # __init__ (the attribute shadows the method on the instance).
    # Any call to buffer.packet_count() therefore raised
    # `TypeError: 'int' object is not callable`, which killed
    # classification before it ever started. Fixed by using the
    # attribute directly everywhere instead of a same-named method.


# ============================================================
# MAIN RYU CONTROLLER
# ============================================================

class QoSRyuController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(QoSRyuController, self).__init__(*args, **kwargs)

        self.flow_buffers = {}
        self.flow_classifications = {}
        self.datapaths = {}
        self.mac_to_port = {}

        self.stats = {
            'flows_classified': 0,
            'policies_applied': 0,
            'api_calls': 0,
            'api_failures': 0,
            'packets_captured': 0,
        }

        self.cleanup_thread = hub.spawn(self._cleanup_loop)

        self.logger.info("=" * 60)
        self.logger.info("QoS Ryu Controller (XGBoost Version)")
        self.logger.info(f"ML API: {ML_API_URL}")
        self.logger.info(f"Min packets: {MIN_PACKETS}")
        self.logger.info(f"Feature timeout: {FEATURE_TIMEOUT}s")
        self.logger.info("=" * 60)

    # ============================================================
    # SWITCH CONNECTION
    # ============================================================

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        self.datapaths[datapath.id] = datapath
        self.mac_to_port.setdefault(datapath.id, {})

        self.logger.info(f"Switch {datapath.id} connected")

        # Default: send all unmatched packets to controller
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]

        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=0,
            match=match,
            instructions=inst
        )
        datapath.send_msg(mod)

        # ARP flood
        match_arp = parser.OFPMatch(eth_type=0x0806)
        actions_arp = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
        inst_arp = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions_arp)]
        mod_arp = parser.OFPFlowMod(
            datapath=datapath,
            priority=1,
            match=match_arp,
            instructions=inst_arp,
            idle_timeout=60,
            hard_timeout=300,
        )
        datapath.send_msg(mod_arp)

        # ICMP flood
        match_icmp = parser.OFPMatch(eth_type=0x0800, ip_proto=1)
        actions_icmp = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
        inst_icmp = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions_icmp)]
        mod_icmp = parser.OFPFlowMod(
            datapath=datapath,
            priority=1,
            match=match_icmp,
            instructions=inst_icmp,
            idle_timeout=60,
            hard_timeout=300,
        )
        datapath.send_msg(mod_icmp)

        self.logger.info("Default flows installed")

    # ============================================================
    # PACKET IN HANDLER
    # ============================================================

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        if not eth:
            return

        dpid = datapath.id
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][eth.src] = in_port

        flow_key, src_ip = self._extract_flow_key(pkt, in_port)

        if flow_key is None:
            self._forward_normal(datapath, in_port, msg.data)
            return

        # Ignore reverse traffic from server
        if flow_key[0] == SERVER_IP:
            self._forward_normal(datapath, in_port, msg.data)
            return

        self.stats['packets_captured'] += 1

        # Already classified?
        if flow_key in self.flow_classifications:
            traffic_class = self.flow_classifications[flow_key]
            self._install_qos_flow(datapath, flow_key, in_port, traffic_class, msg.data, eth.dst)
            return

        # New flow - start buffering
        if flow_key not in self.flow_buffers:
            self.flow_buffers[flow_key] = FlowBuffer(flow_key)
            self.logger.info(f"New flow from {src_ip} (buffers: {len(self.flow_buffers)})")

        buffer = self.flow_buffers[flow_key]
        timestamp = time.time()
        completed = buffer.add_packet(msg.data, timestamp, src_ip)

        # Forward packet normally while collecting
        self._forward_normal(datapath, in_port, msg.data)

        # Classify when complete
        if completed and not buffer.is_dispatched():
            buffer.mark_dispatched()
            self.logger.info(f"Flow collected {buffer.packet_count} packets, classifying...")
            hub.spawn(self._classify_flow, datapath, flow_key, in_port, buffer, eth.dst)

    # ============================================================
    # FLOW KEY EXTRACTION
    # ============================================================

    def _extract_flow_key(self, pkt, in_port):
        ip = pkt.get_protocol(ipv4.ipv4)
        if not ip:
            return None, None

        src_ip = ip.src
        dst_ip = ip.dst
        protocol = ip.proto

        src_port = None
        dst_port = None

        tcp_pkt = pkt.get_protocol(tcp.tcp)
        udp_pkt = pkt.get_protocol(udp.udp)

        if tcp_pkt:
            src_port = tcp_pkt.src_port
            dst_port = tcp_pkt.dst_port
        elif udp_pkt:
            src_port = udp_pkt.src_port
            dst_port = udp_pkt.dst_port

        flow_key = (src_ip, dst_ip, src_port, dst_port, protocol, in_port)
        return flow_key, src_ip

    # ============================================================
    # CLASSIFICATION (Calls FastAPI)
    # ============================================================

    def _classify_flow(self, datapath, flow_key, in_port, buffer, dst_mac=None):
        # Extract 10 features
        features = buffer.extract_features()

        src_ip = flow_key[0] if flow_key else "unknown"
        self.logger.info(f"Classifying flow from {src_ip} ({buffer.packet_count} packets)")

        try:
            self.stats['api_calls'] += 1
            response = requests.post(
                ML_API_URL,
                json={'features': features},
                timeout=5
            )

            if response.status_code == 200:
                result = response.json()
                traffic_class = result.get('traffic_type', 'default').lower()
                confidence = result.get('confidence', 0)

                self.logger.info(f"Flow classified: {traffic_class} (conf: {confidence:.2f})")
                self.stats['flows_classified'] += 1

                self.flow_classifications[flow_key] = traffic_class
                self._install_qos_flow(datapath, flow_key, in_port, traffic_class, None, dst_mac)
            else:
                self.logger.warning(f"API error: {response.status_code}")
                self._install_default_flow(datapath, flow_key, in_port, dst_mac)

        except requests.exceptions.RequestException as e:
            self.logger.warning(f"ML API unreachable: {e}")
            self.stats['api_failures'] += 1
            self._install_default_flow(datapath, flow_key, in_port, dst_mac)

        # Clean up buffer
        if flow_key in self.flow_buffers:
            del self.flow_buffers[flow_key]

    # ============================================================
    # QoS FLOW INSTALLATION
    # ============================================================

    def _install_qos_flow(self, datapath, flow_key, in_port, traffic_class, data, dst_mac=None):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        src_ip, dst_ip, src_port, dst_port, protocol, in_port_val = flow_key
        priority = PRIORITY_MAP.get(traffic_class, 4)

        # Determine output port
        dpid = datapath.id
        out_port = None

        if dst_mac and dpid in self.mac_to_port:
            if dst_mac in self.mac_to_port[dpid]:
                out_port = self.mac_to_port[dpid][dst_mac]

        actions = [parser.OFPActionOutput(out_port)] if out_port is not None else \
                  [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]

        instructions = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]

        # Build matches (both directions)
        if src_port and dst_port and src_port > 0 and dst_port > 0:
            if protocol == 6:  # TCP
                match_fwd = parser.OFPMatch(
                    eth_type=0x0800, ip_proto=6,
                    ipv4_src=src_ip, ipv4_dst=dst_ip,
                    tcp_src=src_port, tcp_dst=dst_port,
                )
                match_rev = parser.OFPMatch(
                    eth_type=0x0800, ip_proto=6,
                    ipv4_src=dst_ip, ipv4_dst=src_ip,
                    tcp_src=dst_port, tcp_dst=src_port,
                )
            elif protocol == 17:  # UDP
                match_fwd = parser.OFPMatch(
                    eth_type=0x0800, ip_proto=17,
                    ipv4_src=src_ip, ipv4_dst=dst_ip,
                    udp_src=src_port, udp_dst=dst_port,
                )
                match_rev = parser.OFPMatch(
                    eth_type=0x0800, ip_proto=17,
                    ipv4_src=dst_ip, ipv4_dst=src_ip,
                    udp_src=dst_port, udp_dst=src_port,
                )
            else:
                match_fwd = parser.OFPMatch(
                    eth_type=0x0800, ip_proto=protocol,
                    ipv4_src=src_ip, ipv4_dst=dst_ip,
                )
                match_rev = parser.OFPMatch(
                    eth_type=0x0800, ip_proto=protocol,
                    ipv4_src=dst_ip, ipv4_dst=src_ip,
                )
        else:
            match_fwd = parser.OFPMatch(
                eth_type=0x0800, ip_proto=protocol,
                ipv4_src=src_ip, ipv4_dst=dst_ip,
            )
            match_rev = parser.OFPMatch(
                eth_type=0x0800, ip_proto=protocol,
                ipv4_src=dst_ip, ipv4_dst=src_ip,
            )

        # Install both directions
        for match in [match_fwd, match_rev]:
            mod = parser.OFPFlowMod(
                datapath=datapath,
                priority=priority,
                match=match,
                instructions=instructions,
                idle_timeout=60,
                hard_timeout=300,
                buffer_id=ofproto.OFP_NO_BUFFER,
            )
            datapath.send_msg(mod)

        self.stats['policies_applied'] += 1
        self.logger.info(f"Installed QoS: {traffic_class} (priority={priority})")

        # Send original packet if needed
        if data:
            out = parser.OFPPacketOut(
                datapath=datapath,
                buffer_id=ofproto.OFP_NO_BUFFER,
                in_port=in_port_val,
                actions=actions,
                data=data
            )
            datapath.send_msg(out)

    def _install_default_flow(self, datapath, flow_key, in_port, dst_mac=None):
        self.flow_classifications[flow_key] = 'default'
        self._install_qos_flow(datapath, flow_key, in_port, 'default', None, dst_mac)

    # ============================================================
    # UTILITY METHODS
    # ============================================================

    def _forward_normal(self, datapath, in_port, data):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=in_port,
            actions=actions,
            data=data
        )
        datapath.send_msg(out)

    def _cleanup_loop(self):
        while True:
            hub.sleep(1)
            expired = []
            for flow_key, buffer in self.flow_buffers.items():
                if buffer.is_expired():
                    expired.append(flow_key)

            for flow_key in expired:
                self.logger.warning(f"Flow expired: {flow_key[0]}")
                del self.flow_buffers[flow_key]

    @set_ev_cls(ofp_event.EventOFPErrorMsg, MAIN_DISPATCHER)
    def error_handler(self, ev):
        self.logger.error(f"OpenFlow error: {ev.msg}")

    def get_stats(self):
        return {
            'flows_classified': self.stats['flows_classified'],
            'policies_applied': self.stats['policies_applied'],
            'api_calls': self.stats['api_calls'],
            'api_failures': self.stats['api_failures'],
            'packets_captured': self.stats['packets_captured'],
            'active_buffers': len(self.flow_buffers),
            'classified_flows': len(self.flow_classifications),
        }
