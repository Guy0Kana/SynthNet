#!/usr/bin/env python3

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ipv4, tcp, udp
from ryu.lib import hub

import requests
import time
from collections import Counter

# ── Config ────────────────────────────────────────────────────────────────────
ML_API_URL       = "http://localhost:8000/classify"
FLOW_SAMPLES     = 30
MIN_PACKETS      = 10   # classify after this many if half-timeout reached
FEATURE_TIMEOUT  = 60
SERVER_IP        = "10.0.0.10"

# TCP flag bitmasks
TCP_FIN = 0x01
TCP_SYN = 0x02
TCP_RST = 0x04
TCP_PSH = 0x08
TCP_ACK = 0x10
TCP_URG = 0x20
TCP_ECE = 0x40
TCP_CWR = 0x80

# QoS Priority Mapping (higher number = higher priority)
# Must match XGBoost labels
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


# ── TCP flag extraction ───────────────────────────────────────────────────────

def extract_tcp_flags(tcp_pkt):
    flags = []
    if not tcp_pkt:
        return flags
    bits = tcp_pkt.bits
    if bits & TCP_CWR: flags.append('CWR')
    if bits & TCP_ECE: flags.append('ECE')
    if bits & TCP_URG: flags.append('URG')
    if bits & TCP_ACK: flags.append('ACK')
    if bits & TCP_PSH: flags.append('PSH')
    if bits & TCP_RST: flags.append('RST')
    if bits & TCP_SYN: flags.append('SYN')
    if bits & TCP_FIN: flags.append('FIN')
    return flags


# ── FlowBuffer ────────────────────────────────────────────────────────────────

class FlowBuffer:
    def __init__(self, flow_key, timeout=FEATURE_TIMEOUT):
        self.flow_key      = flow_key
        self.packets       = []
        self.start_time    = time.time()
        self.timeout       = timeout
        self.completed     = False
        self.origin_src    = None
        self.src_ip        = None

        # Byte / packet counters
        self.bytes_fwd   = 0
        self.bytes_rev   = 0
        self.packets_fwd = 0
        self.packets_rev = 0

        # Timestamps per direction (for IAT computation)
        self.all_timestamps = []
        self.fwd_timestamps = []
        self.rev_timestamps = []

        # TCP flags
        self.tcp_flags_fwd = Counter()
        self.tcp_flags_rev = Counter()

        # Active / idle tracking
        self.active_periods      = []
        self.idle_periods        = []
        self.current_active_start = None
        self.IDLE_THRESHOLD      = 1.0   # seconds

        self.ppi_roundtrips = 0
        self.last_dir       = None

    # ── add_packet ────────────────────────────────────────────────────────────
    def add_packet(self, pkt_data, timestamp, src_ip, tcp_flags=None):
        if not self.packets:
            self.origin_src = src_ip
            self.src_ip     = src_ip

        direction  = 1 if src_ip == self.origin_src else -1
        pkt_size   = len(pkt_data)

        # Byte / packet counts
        if direction == 1:
            self.bytes_fwd   += pkt_size
            self.packets_fwd += 1
        else:
            self.bytes_rev   += pkt_size
            self.packets_rev += 1

        # TCP flags
        if tcp_flags:
            target = self.tcp_flags_fwd if direction == 1 else self.tcp_flags_rev
            for flag in tcp_flags:
                target[flag] += 1

        # Roundtrip detection
        if self.last_dir is not None and self.last_dir != direction:
            self.ppi_roundtrips += 1
        self.last_dir = direction

        # Active / idle tracking
        if self.all_timestamps:
            gap = timestamp - self.all_timestamps[-1]
            if gap > self.IDLE_THRESHOLD:
                self.idle_periods.append(gap)
                if self.current_active_start is not None:
                    self.active_periods.append(
                        self.all_timestamps[-1] - self.current_active_start
                    )
                self.current_active_start = timestamp
            else:
                if self.current_active_start is None:
                    self.current_active_start = timestamp
        else:
            self.current_active_start = timestamp

        # Timestamps
        self.all_timestamps.append(timestamp)
        if direction == 1:
            self.fwd_timestamps.append(timestamp)
        else:
            self.rev_timestamps.append(timestamp)

        # PPI packet record
        self.packets.append({
            'size':      pkt_size,
            'timestamp': timestamp,
            'dir':       direction,
        })

        if len(self.packets) >= FLOW_SAMPLES:
            self.completed = True
            return True
        return False

    # ── Timers ────────────────────────────────────────────────────────────────
    def is_expired(self):
        return (time.time() - self.start_time) > self.timeout

    def is_half_expired(self):
        return (time.time() - self.start_time) > (self.timeout / 2)

    def packet_count(self):
        return len(self.packets)

    # ── Feature helpers ───────────────────────────────────────────────────────
    @staticmethod
    def _iat_stats(timestamps):
        """Return (total, min, max, mean, std) IAT in microseconds."""
        if len(timestamps) < 2:
            return 0.0, 0.0, 0.0, 0.0, 0.0
        iats = [(timestamps[i] - timestamps[i-1]) * 1e6
                for i in range(1, len(timestamps))]
        total  = sum(iats)
        mn     = min(iats)
        mx     = max(iats)
        mean   = total / len(iats)
        var    = sum((x - mean)**2 for x in iats) / len(iats)
        std    = var ** 0.5
        return total, mn, mx, mean, std

    # ── get_flowstats — matches ISCX feature set ──────────────────────────────
    def get_flowstats(self):
        duration = max(time.time() - self.start_time, 0.001)

        total_pkts  = self.packets_fwd + self.packets_rev
        total_bytes = self.bytes_fwd   + self.bytes_rev

        # All-flow IAT
        _, min_flowiat, max_flowiat, mean_flowiat, std_flowiat = \
            self._iat_stats(self.all_timestamps)

        # Forward IAT
        total_fiat, min_fiat, max_fiat, mean_fiat, _ = \
            self._iat_stats(self.fwd_timestamps)

        # Backward IAT
        total_biat, min_biat, max_biat, mean_biat, _ = \
            self._iat_stats(self.rev_timestamps)

        # Active / idle stats
        def _stats(lst):
            if not lst:
                return 0.0, 0.0, 0.0, 0.0
            mn   = min(lst)
            mx   = max(lst)
            mean = sum(lst) / len(lst)
            var  = sum((x - mean)**2 for x in lst) / len(lst)
            return mn, mx, mean, var**0.5

        min_active, max_active, mean_active, std_active = _stats(self.active_periods)
        min_idle,   max_idle,   mean_idle,   std_idle   = _stats(self.idle_periods)

        return {
            # Core timing
            'duration':            duration,
            'total_fiat':          total_fiat,
            'total_biat':          total_biat,
            'min_fiat':            min_fiat,
            'min_biat':            min_biat,
            'max_fiat':            max_fiat,
            'max_biat':            max_biat,
            'mean_fiat':           mean_fiat,
            'mean_biat':           mean_biat,
            # Flow IAT
            'min_flowiat':         min_flowiat,
            'max_flowiat':         max_flowiat,
            'mean_flowiat':        mean_flowiat,
            'std_flowiat':         std_flowiat,
            # Throughput
            'flowPktsPerSecond':   total_pkts  / duration,
            'flowBytesPerSecond':  total_bytes / duration,
            # Active / idle
            'min_active':          min_active,
            'mean_active':         mean_active,
            'max_active':          max_active,
            'std_active':          std_active,
            'min_idle':            min_idle,
            'mean_idle':           mean_idle,
            'max_idle':            max_idle,
            'std_idle':            std_idle,
        }


# ── Controller ────────────────────────────────────────────────────────────────

class QoSRyuController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.flow_buffers        = {}
        self.flow_classifications = {}
        self.mac_to_port         = {}
        self.datapaths           = {}
        self._classifying        = set()

        self.stats = {
            'packets_captured':  0,
            'flows_classified':  0,
            'policies_applied':  0,
            'api_calls':         0,
            'api_failures':      0,
        }

        self.cleanup_thread = hub.spawn(self._cleanup_loop)

        self.logger.info("=" * 60)
        self.logger.info("QoS Ryu Controller — XGBoost ML backend")
        self.logger.info(f"ML API:   {ML_API_URL}")
        self.logger.info(f"Samples:  {FLOW_SAMPLES} packets (min {MIN_PACKETS})")
        self.logger.info(f"Timeout:  {FEATURE_TIMEOUT}s")
        self.logger.info("=" * 60)

    # ── Switch connect ────────────────────────────────────────────────────────
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto  = datapath.ofproto
        parser   = datapath.ofproto_parser

        self.datapaths[datapath.id] = datapath
        self.mac_to_port.setdefault(datapath.id, {})

        self.logger.info(f"Switch {datapath.id} connected")

        # Default table-miss → controller
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(
            ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER
        )]
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        datapath.send_msg(parser.OFPFlowMod(
            datapath=datapath, priority=0,
            match=match, instructions=inst
        ))

        # ARP flood
        datapath.send_msg(parser.OFPFlowMod(
            datapath=datapath, priority=1,
            match=parser.OFPMatch(eth_type=0x0806),
            instructions=[parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS,
                [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
            )],
            idle_timeout=0, hard_timeout=0,
        ))

        # ICMP flood
        datapath.send_msg(parser.OFPFlowMod(
            datapath=datapath, priority=1,
            match=parser.OFPMatch(eth_type=0x0800, ip_proto=1),
            instructions=[parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS,
                [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
            )],
            idle_timeout=0, hard_timeout=0,
        ))

        self.logger.info("Default, ARP, ICMP flows installed")

    # ── Packet-in ─────────────────────────────────────────────────────────────
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg      = ev.msg
        datapath = msg.datapath
        ofproto  = datapath.ofproto
        parser   = datapath.ofproto_parser
        in_port  = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if not eth:
            return

        # MAC learning
        dpid = datapath.id
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][eth.src] = in_port

        # Extract flow key
        flow_key, src_ip, tcp_flags = self._extract_flow_key(pkt, in_port)

        if flow_key is None:
            self._forward_normal(datapath, in_port, msg.data)
            return

        # Ignore server → host traffic
        if src_ip == SERVER_IP:
            self._forward_normal(datapath, in_port, msg.data)
            return

        self.stats['packets_captured'] += 1

        # Already classified — reinstall rule and forward
        if flow_key in self.flow_classifications:
            self._install_qos_flow(
                datapath, flow_key, in_port,
                self.flow_classifications[flow_key],
                msg.data, eth.dst
            )
            return

        # Buffer packets
        if flow_key not in self.flow_buffers:
            self.flow_buffers[flow_key] = FlowBuffer(flow_key)
            self.logger.info(
                f"New flow: {src_ip} → {flow_key[1]} "
                f"(active buffers: {len(self.flow_buffers)})"
            )

        buf       = self.flow_buffers[flow_key]
        timestamp = time.time()
        completed = buf.add_packet(msg.data, timestamp, src_ip, tcp_flags)

        # Forward while buffering
        self._forward_normal(datapath, in_port, msg.data)

        self.logger.debug(
            f"Buffer {src_ip}: {buf.packet_count()}/{FLOW_SAMPLES}"
        )

        # Trigger classification when enough data collected
        should_classify = (
            completed or
            (buf.packet_count() >= MIN_PACKETS and buf.is_half_expired())
        )

        if should_classify and flow_key not in self._classifying:
            self._classifying.add(flow_key)
            hub.spawn(
                self._classify_flow,
                datapath, flow_key, in_port, buf, eth.dst
            )

    # ── Extract flow key ──────────────────────────────────────────────────────
    def _extract_flow_key(self, pkt, in_port):
        ip = pkt.get_protocol(ipv4.ipv4)
        if not ip:
            return None, None, None

        src_ip   = ip.src
        dst_ip   = ip.dst
        protocol = ip.proto
        src_port = None
        dst_port = None
        tcp_flags = None

        tcp_pkt = pkt.get_protocol(tcp.tcp)
        udp_pkt = pkt.get_protocol(udp.udp)

        if tcp_pkt:
            src_port  = tcp_pkt.src_port
            dst_port  = tcp_pkt.dst_port
            tcp_flags = extract_tcp_flags(tcp_pkt)
        elif udp_pkt:
            src_port = udp_pkt.src_port
            dst_port = udp_pkt.dst_port

        flow_key = (src_ip, dst_ip, src_port, dst_port, protocol, in_port)
        return flow_key, src_ip, tcp_flags

    # ── Classify flow ─────────────────────────────────────────────────────────
    def _classify_flow(self, datapath, flow_key, in_port, buffer, dst_mac=None):
        src_ip = flow_key[0]

        try:
            flowstats = buffer.get_flowstats()

            self.logger.info(
                f"Classifying {src_ip} "
                f"({buffer.packet_count()} packets, "
                f"{flowstats['duration']:.1f}s)"
            )

            # Build the 10 features list for XGBoost
            features_list = [
                flowstats['duration'],
                flowstats['total_fiat'],
                flowstats['total_biat'],
                flowstats['min_fiat'],
                flowstats['min_biat'],
                flowstats['max_fiat'],
                flowstats['max_biat'],
                flowstats['mean_biat'],
                flowstats['max_flowiat'],
                flowstats['mean_flowiat'],
            ]

            self.logger.debug(f"Features: {features_list}")

            self.stats['api_calls'] += 1
            response = requests.post(
                ML_API_URL,
                json={'features': features_list},
                timeout=3,
            )

            if response.status_code == 200:
                result = response.json()
                # XGBoost returns 'traffic_type', not 'category'
                traffic_class = result.get('traffic_type', 'default')
                confidence = result.get('confidence', 0.0)

                self.logger.info(
                    f"  → {traffic_class} "
                    f"(confidence={confidence:.3f})"
                )
                self.stats['flows_classified'] += 1
                self.flow_classifications[flow_key] = traffic_class

                self._install_qos_flow(
                    datapath, flow_key, in_port,
                    traffic_class, None, dst_mac
                )
            else:
                self.logger.warning(f"API returned {response.status_code}")
                self._install_default_flow(datapath, flow_key, in_port, dst_mac)

        except requests.exceptions.RequestException as e:
            self.logger.warning(f"ML backend unreachable: {e}")
            self.stats['api_failures'] += 1
            self._install_default_flow(datapath, flow_key, in_port, dst_mac)

        finally:
            self._classifying.discard(flow_key)
            if flow_key in self.flow_buffers:
                del self.flow_buffers[flow_key]

    # ── Install QoS flow rule ─────────────────────────────────────────────────
    def _install_qos_flow(self, datapath, flow_key, in_port,
                          traffic_class, data, dst_mac=None):
        ofproto  = datapath.ofproto
        parser   = datapath.ofproto_parser
        dpid     = datapath.id

        src_ip, dst_ip, src_port, dst_port, protocol, in_port_val = flow_key

        # Build match inline (no set_tcp_src/dst — avoids OXM ordering errors)
        if src_port and dst_port:
            if protocol == 6:
                match = parser.OFPMatch(
                    eth_type=0x0800, ip_proto=6,
                    ipv4_src=src_ip, ipv4_dst=dst_ip,
                    tcp_src=src_port, tcp_dst=dst_port,
                )
            elif protocol == 17:
                match = parser.OFPMatch(
                    eth_type=0x0800, ip_proto=17,
                    ipv4_src=src_ip, ipv4_dst=dst_ip,
                    udp_src=src_port, udp_dst=dst_port,
                )
            else:
                match = parser.OFPMatch(
                    eth_type=0x0800, ip_proto=protocol,
                    ipv4_src=src_ip, ipv4_dst=dst_ip,
                )
        else:
            match = parser.OFPMatch(
                eth_type=0x0800, ip_proto=protocol,
                ipv4_src=src_ip, ipv4_dst=dst_ip,
            )

        # Resolve output port from MAC table
        out_port = None
        if dst_mac and dpid in self.mac_to_port:
            out_port = self.mac_to_port[dpid].get(dst_mac)

        if out_port is None:
            # MAC not yet learned — flood this packet, don't install rule
            self.logger.debug(
                f"MAC {dst_mac} not learned yet — flooding"
            )
            if data:
                self._forward_normal(datapath, in_port_val, data)
            return

        priority     = PRIORITY_MAP.get(traffic_class, 4)
        final_action = parser.OFPActionOutput(out_port)

        instructions = [parser.OFPInstructionActions(
            ofproto.OFPIT_APPLY_ACTIONS, [final_action]
        )]

        datapath.send_msg(parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=instructions,
            idle_timeout=60,
            hard_timeout=300,
            buffer_id=ofproto.OFP_NO_BUFFER,
        ))

        self.stats['policies_applied'] += 1
        self.logger.info(
            f"QoS rule installed: {src_ip} → {traffic_class} "
            f"(priority={priority}, out_port={out_port})"
        )

        # Send triggering packet out
        if data:
            datapath.send_msg(parser.OFPPacketOut(
                datapath=datapath,
                buffer_id=ofproto.OFP_NO_BUFFER,
                in_port=in_port_val,
                actions=[final_action],
                data=data,
            ))

    # ── Default flow ──────────────────────────────────────────────────────────
    def _install_default_flow(self, datapath, flow_key, in_port, dst_mac=None):
        self.flow_classifications[flow_key] = 'default'
        self._install_qos_flow(
            datapath, flow_key, in_port, 'default', None, dst_mac
        )

    # ── Forward normal (packet-out only, no flow rule) ────────────────────────
    def _forward_normal(self, datapath, in_port, data):
        ofproto = datapath.ofproto
        parser  = datapath.ofproto_parser

        pkt = packet.Packet(data)
        eth = pkt.get_protocol(ethernet.ethernet)

        if eth and eth.ethertype != 0x0800:
            # Non-IP — install a persistent flood rule to reduce controller load
            match = parser.OFPMatch(eth_type=eth.ethertype)
            datapath.send_msg(parser.OFPFlowMod(
                datapath=datapath, priority=1,
                match=match,
                instructions=[parser.OFPInstructionActions(
                    ofproto.OFPIT_APPLY_ACTIONS,
                    [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
                )],
                idle_timeout=60, hard_timeout=300,
            ))

        # Always flood the current packet
        datapath.send_msg(parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=in_port,
            actions=[parser.OFPActionOutput(ofproto.OFPP_FLOOD)],
            data=data,
        ))

    # ── Cleanup loop ──────────────────────────────────────────────────────────
    def _cleanup_loop(self):
        while True:
            hub.sleep(2)
            expired = [
                (k, v) for k, v in self.flow_buffers.items()
                if v.is_expired()
            ]
            for flow_key, buf in expired:
                self.logger.warning(
                    f"Flow {buf.src_ip} expired with "
                    f"{buf.packet_count()} packets"
                )
                self._classifying.discard(flow_key)
                del self.flow_buffers[flow_key]

    # ── Error handler ─────────────────────────────────────────────────────────
    @set_ev_cls(ofp_event.EventOFPErrorMsg, MAIN_DISPATCHER)
    def error_handler(self, ev):
        self.logger.error(f"OpenFlow error: {ev.msg}")

    # ── Stats ─────────────────────────────────────────────────────────────────
    def get_stats(self):
        return {
            **self.stats,
            'active_buffers':    len(self.flow_buffers),
            'classified_flows':  len(self.flow_classifications),
        }
