#!/usr/bin/env python3

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ipv4, tcp, udp
from ryu.lib import hub

import requests
import json
import time
from collections import defaultdict, Counter


ML_API_URL = "http://localhost:8000/classify"
FLOW_SAMPLES = 30
FEATURE_TIMEOUT = 5

TCP_FIN = 0x01
TCP_SYN = 0x02
TCP_RST = 0x04
TCP_PSH = 0x08
TCP_ACK = 0x10
TCP_URG = 0x20
TCP_ECE = 0x40
TCP_CWR = 0x80

# QoS Priority Mapping (higher number = higher priority)
PRIORITY_MAP = {
    'voip': 10,
    'cloud_email': 9,
    'dns': 8,
    'http': 5,
    'web': 5,
    'realtime': 5,
    'video': 2,
    'ftp': 2,
    'background': 1,
    'p2p': 1,
    'default': 4,
}

# Meter Mapping (1 = highest priority queue, 4 = lowest)
METER_MAP = {
    'voip': 1,
    'cloud_email': 1,
    'dns': 2,
    'http': 2,
    'web': 2,
    'realtime': 2,
    'video': 3,
    'ftp': 3,
    'background': 4,
    'p2p': 4,
    'default': 4,
}


def extract_tcp_flags(tcp_pkt):
    flags = []
    if tcp_pkt:
        bits = tcp_pkt.bits
        if bits & TCP_CWR:
            flags.append('CWR')
        if bits & TCP_ECE:
            flags.append('ECE')
        if bits & TCP_URG:
            flags.append('URG')
        if bits & TCP_ACK:
            flags.append('ACK')
        if bits & TCP_PSH:
            flags.append('PSH')
        if bits & TCP_RST:
            flags.append('RST')
        if bits & TCP_SYN:
            flags.append('SYN')
        if bits & TCP_FIN:
            flags.append('FIN')
    return flags


class FlowBuffer:
    def __init__(self, flow_key, timeout=FEATURE_TIMEOUT):
        self.flow_key = flow_key
        self.packets = []
        self.start_time = time.time()
        self.timeout = timeout
        self.completed = False
        self.origin_src = None
        self.src_ip = None

        # FIXED: Proper indentation (8 spaces)
        self.bytes_fwd = 0
        self.bytes_rev = 0
        self.packets_fwd = 0
        self.packets_rev = 0

        # TCP flags per direction
        self.tcp_flags_fwd = Counter()
        self.tcp_flags_rev = Counter()

        # PPI sequence tracking
        self.ppi_roundtrips = 0
        self.last_dir = None

    def add_packet(self, pkt_data, timestamp, src_ip, tcp_flags=None):
        if not self.packets:
            self.origin_src = src_ip
            self.src_ip = src_ip

        direction = 1 if src_ip == self.origin_src else -1

        pkt_size = len(pkt_data)
        if direction == 1:
            self.bytes_fwd += pkt_size
            self.packets_fwd += 1
        else:
            self.bytes_rev += pkt_size
            self.packets_rev += 1

        if tcp_flags:
            if direction == 1:
                for flag in tcp_flags:
                    self.tcp_flags_fwd[flag] += 1
            else:
                for flag in tcp_flags:
                    self.tcp_flags_rev[flag] += 1

        if self.last_dir is not None and self.last_dir != direction:
            self.ppi_roundtrips += 1

        self.last_dir = direction

        self.packets.append({
            'size': len(pkt_data),
            'timestamp': timestamp,
            'dir': direction
        })

        if len(self.packets) >= FLOW_SAMPLES:
            self.completed = True
            return True
        return False

    def is_expired(self):
        return (time.time() - self.start_time) > self.timeout

    def get_flowstats(self):
        duration = time.time() - self.start_time
        if duration < 0.001:
            duration = 0.001

        if len(self.packets) >= 2:
            ppi_duration = self.packets[-1]['timestamp'] - self.packets[0]['timestamp']
        else:
            ppi_duration = 0

        flowstats = [
            self.bytes_fwd,
            self.bytes_rev,
            self.packets_fwd,
            self.packets_rev,
            duration,
            len(self.packets),
            self.ppi_roundtrips,
            ppi_duration,
            self.tcp_flags_fwd.get('CWR', 0),
            self.tcp_flags_rev.get('CWR', 0),
            self.tcp_flags_fwd.get('ECE', 0),
            self.tcp_flags_rev.get('ECE', 0),
            self.tcp_flags_rev.get('PSH', 0),
            self.tcp_flags_fwd.get('RST', 0),
            self.tcp_flags_rev.get('RST', 0),
            self.tcp_flags_fwd.get('FIN', 0),
            self.tcp_flags_rev.get('FIN', 0),
        ]

        return flowstats

    def extract_ppi_features(self):
        if len(self.packets) < 5:
            return None

        sizes = [p['size'] for p in self.packets]

        timestamps = [p['timestamp'] for p in self.packets]
        ipts = [0]
        for i in range(1, len(timestamps)):
            ipt_us = (timestamps[i] - timestamps[i-1]) * 1_000_000
            ipts.append(int(ipt_us))

        dirs = [p['dir'] for p in self.packets]

        while len(sizes) < FLOW_SAMPLES:
            sizes.append(0)
            ipts.append(0)
            dirs.append(0)

        sizes = sizes[:FLOW_SAMPLES]
        ipts = ipts[:FLOW_SAMPLES]
        dirs = dirs[:FLOW_SAMPLES]

        return {
            'sizes': sizes,
            'ipts': ipts,
            'dirs': dirs
        }

    def packet_count(self):
        return len(self.packets)


class QoSRyuController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(QoSRyuController, self).__init__(*args, **kwargs)

        self.flow_buffers = {}
        self.flow_classifications = {}

        self.stats = {
            'flows_classified': 0,
            'policies_applied': 0,
            'api_calls': 0,
            'api_failures': 0,
            'packets_captured': 0,
        }

        self.cleanup_thread = hub.spawn(self._cleanup_loop)

        self.logger.info("QoS Ryu Controller initialized")
        self.logger.info(f"ML API endpoint: {ML_API_URL}")
        self.logger.info(f"Collecting first {FLOW_SAMPLES} packets per flow")
        self.logger.info("=" * 60)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        self.logger.info(f"Switch {datapath.id} connected")

        self._install_meters(datapath)

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

        # ARP flood flow (for ping to work)
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

        # ICMP flood flow (for ping to work)
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

        self.logger.info("Default ARP, ICMP  flows installed")

    def _install_meters(self, datapath):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        meter_configs = [
            (1, 1000000, 100000),
            (2, 500000, 50000),
            (3, 100000, 10000),
            (4, 50000, 5000),
        ]

        for meter_id, rate, burst in meter_configs:
            meter_mod = parser.OFPMeterMod(
                datapath=datapath,
                command=ofproto.OFPMC_ADD,
                flags=ofproto.OFPMF_KBPS,
                meter_id=meter_id,
                bands=[parser.OFPMeterBandDrop(rate=rate, burst_size=burst)]
            )
            datapath.send_msg(meter_mod)

        self.logger.info("Meter tables installed (IDs 1-4)")

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

        # FIXED: Changed to _extract_flow_key (method exists)
        flow_key, src_ip, tcp_flags = self._extract_flow_key(pkt, in_port)

        if flow_key is None:
            self._forward_normal(datapath, in_port, msg.data)
            return

        self.stats['packets_captured'] += 1

        if flow_key in self.flow_classifications:
            traffic_class = self.flow_classifications[flow_key]
            self._install_qos_flow(datapath, flow_key, in_port, traffic_class, msg.data)
            return

        if flow_key not in self.flow_buffers:
            self.flow_buffers[flow_key] = FlowBuffer(flow_key)
            self.logger.info(f"New flow detected: {flow_key[:2]}... (total packets: {len(self.flow_buffers)})")

        buffer = self.flow_buffers[flow_key]
        timestamp = time.time()
        completed = buffer.add_packet(msg.data, timestamp, src_ip, tcp_flags)

        self.logger.debug(f"Flow buffer: {buffer.packet_count()}/{FLOW_SAMPLES} packets")

        if completed:
            hub.spawn(self._classify_flow, datapath, flow_key, in_port, buffer)

    def _extract_flow_key(self, pkt, in_port):
        ip = pkt.get_protocol(ipv4.ipv4)
        if not ip:
            return None, None, None

        src_ip = ip.src
        dst_ip = ip.dst
        protocol = ip.proto

        src_port = None
        dst_port = None
        tcp_flags = None

        tcp_pkt = pkt.get_protocol(tcp.tcp)
        udp_pkt = pkt.get_protocol(udp.udp)

        if tcp_pkt:
            src_port = tcp_pkt.src_port
            dst_port = tcp_pkt.dst_port
            tcp_flags = extract_tcp_flags(tcp_pkt)
        elif udp_pkt:
            src_port = udp_pkt.src_port
            dst_port = udp_pkt.dst_port

        flow_key = (src_ip, dst_ip, src_port, dst_port, protocol, in_port)
        return flow_key, src_ip, tcp_flags

    def _classify_flow(self, datapath, flow_key, in_port, buffer):
        ppi_features = buffer.extract_ppi_features()
        flowstats = buffer.get_flowstats()

        if not ppi_features:
            self.logger.warning(f"Insufficient features for {flow_key[:2]}, using default")
            hub.spawn(self._install_default_flow, datapath, flow_key, in_port)
            del self.flow_buffers[flow_key]
            return

        # FIXED: Changed 'features' to 'ppi_features'
        api_payload = {
            'sizes': ppi_features['sizes'],
            'ipts': ppi_features['ipts'],
            'dirs': ppi_features['dirs'],
            'flowstats': flowstats
        }

        src_ip = flow_key[0] if flow_key else "unknown"
        self.logger.info(f"Classifying flow from {src_ip} ({buffer.packet_count()} packets collected)")

        try:
            self.stats['api_calls'] += 1
            response = requests.post(
                ML_API_URL,
                json=api_payload,
                timeout=2
            )

            if response.status_code == 200:
                result = response.json()
                traffic_class = result.get('category', 'default')
                confidence = result.get('confidence', 0)

                self.logger.info(f"Flow classified as: {traffic_class} (confidence: {confidence:.2f})")
                self.stats['flows_classified'] += 1

                self.flow_classifications[flow_key] = traffic_class
                hub.spawn(self._install_qos_flow, datapath, flow_key, in_port, traffic_class, None)
            else:
                self.logger.warning(f"API error: {response.status_code}")
                hub.spawn(self._install_default_flow, datapath, flow_key, in_port)

        except requests.exceptions.RequestException as e:
            self.logger.warning(f"ML backend unreachable: {e}")
            self.stats['api_failures'] += 1
            hub.spawn(self._install_default_flow, datapath, flow_key, in_port)

        if flow_key in self.flow_buffers:
            del self.flow_buffers[flow_key]

    def _install_qos_flow(self, datapath, flow_key, in_port, traffic_class, data):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        src_ip, dst_ip, src_port, dst_port, protocol, in_port_val = flow_key

        match = parser.OFPMatch(
            eth_type=0x0800,
            ip_proto=protocol,
            ipv4_src=src_ip,
            ipv4_dst=dst_ip,
        )

        if src_port and dst_port:
            if protocol == 6:
                match.set_tcp_src(src_port)
                match.set_tcp_dst(dst_port)
            elif protocol == 17:
                match.set_udp_src(src_port)
                match.set_udp_dst(dst_port)

        priority = PRIORITY_MAP.get(traffic_class, 4)
        meter_id = METER_MAP.get(traffic_class, 2)

        actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]

        instructions = [
            parser.OFPInstructionMeter(meter_id),
            parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)
        ]

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
        self.logger.info(f"Installed QoS flow: {traffic_class} (priority={priority}, meter={meter_id})")

    def _install_default_flow(self, datapath, flow_key, in_port):
        self.flow_classifications[flow_key] = 'default'
        self._install_qos_flow(datapath, flow_key, in_port, 'default', None)

    def _forward_normal(self, datapath, in_port, data):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        pkt = packet.Packet(data)
        eth = pkt.get_protocol(ethernet.ethernet)

        if eth:
            eth_type = eth.ethertype

            #Flood flows for non-IPv4 traffic
            if eth_type != 0x0800:
                match = parser.OFPMatch(eth_type=eth_type)
                actions = [parser.OFPActionOutput(ofproto.OFP_FLOOD)]
                instructions = [parser.OFPInstructions(ofproto.OFPIT_APPLY_ACTIONS, actiond)]
                mod = parser.OFPFlowMod(
                    datapath=datapath,
                    priority=1,
                    match=match,
                    instructions=instructions,
                    idle_timeout=60,
                    hard_timeout=300,
                )
                datapath.send_msg(mod)
                self.logger.debug(f"Installed flood flow for eth_type=0x{eth_type:04x}")


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
                    expired.append((flow_key, buffer))

            # FIXED: Proper indentation (not inside if)
            for flow_key, buffer in expired:
                src_ip = buffer.src_ip if buffer.src_ip else "unknown"
                self.logger.warning(f"Flow from {src_ip} expired with {buffer.packet_count()} packets")
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
