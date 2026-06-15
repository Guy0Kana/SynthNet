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
from collections import defaultdict


ML_API_URL = "http://localhost:8000/classify" #to be changed
FLOW_SAMPLES = 30          # Number of packets to sample per flow
FEATURE_TIMEOUT = 5        # Seconds to wait for flow completion
METER_BANDS = {
    'high': {'rate': 1000000, 'burst': 100000},    # 1 Gbps (no limit)
    'medium': {'rate': 500000, 'burst': 50000},    # 500 Mbps
    'low': {'rate': 100000, 'burst': 10000},       # 100 Mbps
}

# QoS Priority Mapping (lower number = higher priority)
PRIORITY_MAP = {
    'voip': 10,
    'cloud_email': 8,
    'realtime': 8,
    'http': 5,
    'web': 5,
    'video': 3,
    'ftp': 3,
    'background': 2,
    'p2p': 2,
    'default': 4,
}

# Meter Mapping (for OpenFlow queues)
METER_MAP = {
    'voip': 1,
    'cloud_email': 1,
    'realtime': 1,
    'http': 2,
    'web': 2,
    'video': 2,
    'ftp': 3,
    'background': 3,
    'p2p': 3,
    'default': 2,
}


class FlowBuffer:
    def __init__(self, flow_key, timeout=FEATURE_TIMEOUT):
        self.flow_key = flow_key
        self.packets = []
        self.start_time = time.time()
        self.timeout = timeout
        self.completed = False
        self.origin_src = None
        
    def add_packet(self, pkt_data, timestamp, src_ip):
        if not self.packets:
            self.origin_src = src_ip

        direction = 1 if src_ip == self.origin_src else -1

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
    
    def extract_features(self):
        if len(self.packets) < 5:
            return None
        
        # Packet sizes
        sizes = [p['size'] for p in self.packets]
        
        # Extract inter-packet times (microseconds)
        timestamps = [p['timestamp'] for p in self.packets]
        ipts = [0]  # First packet has no previous packet
        for i in range(1, len(timestamps)):
            ipt_us = (timestamps[i] - timestamps[i-1]) * 1_000_000
            ipts.append(int(ipt_us))
        
        # Extract directions
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
        
        # Flow buffers for new flows
        self.flow_buffers = {}
        self.flow_classifications = {}
        
        # Statistics
        self.stats = {
            'flows_classified': 0,
            'policies_applied': 0,
            'api_calls': 0,
            'api_failures': 0,
            'packets_captured': 0,
        }
        
        # Start background thread for cleanup
        self.cleanup_thread = hub.spawn(self._cleanup_loop)
        
        self.logger.info("QoS Ryu Controller initialized")
        self.logger.info(f"ML API endpoint: {ML_API_URL}")
        self.logger.info(f"Collecting first {FLOW_SAMPLES} packets per flow")
        

    # Switch Connection Handler
    
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """
        Handle switch connection and install default table-miss flow.
        """
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        
        self.logger.info(f"Switch {datapath.id} connected")
        
        # Create meters for QoS
        self._install_meters(datapath)
        
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
        
        self.logger.info("Default flow installed (send to controller)")
    
    def _install_meters(self, datapath):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        
        # Meter for high priority (VoIP, Video)
        meter_mod_high = parser.OFPMeterMod(
            datapath=datapath,
            command=ofproto.OFPMC_ADD,
            flags=ofproto.OFPMF_KBPS,
            meter_id=1,
            bands=[parser.OFPMeterBandDrop(rate=1000000, burst_size=100000)]
        )
        
        # Meter for medium priority (Web, Cloud)
        meter_mod_medium = parser.OFPMeterMod(
            datapath=datapath,
            command=ofproto.OFPMC_ADD,
            flags=ofproto.OFPMF_KBPS,
            meter_id=2,
            bands=[parser.OFPMeterBandDrop(rate=500000, burst_size=50000)]
        )
        
        # Meter for low priority (FTP, Background)
        meter_mod_low = parser.OFPMeterMod(
            datapath=datapath,
            command=ofproto.OFPMC_ADD,
            flags=ofproto.OFPMF_KBPS,
            meter_id=3,
            bands=[parser.OFPMeterBandDrop(rate=100000, burst_size=10000)]
        )
        
        datapath.send_msg(meter_mod_high)
        datapath.send_msg(meter_mod_medium)
        datapath.send_msg(meter_mod_low)
        
        self.logger.info("Meter tables installed")
    
  
    # Packet-In Handler
    
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        """
        Handle incoming packets from the switch.
        """
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']
        
        # Parse packet
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        
        if not eth:
            return
        
        # Extract L3/L4 info
        flow_key, src_ip = self._extract_flow_key(pkt, in_port)
        
        if flow_key is None:
            # Non-IP traffic (ARP, etc.) - forward normally
            self._forward_normal(datapath, in_port, msg.data)
            return
        
        self.stats['packets_captured'] += 1
        
        # Check if flow is already classified
        if flow_key in self.flow_classifications:
            # Flow already classified — install flow rule
            traffic_class = self.flow_classifications[flow_key]
            self._install_qos_flow(datapath, flow_key, in_port, traffic_class, msg.data)
            return
        
        # New flow — buffer packets for classification
        if flow_key not in self.flow_buffers:
            self.flow_buffers[flow_key] = FlowBuffer(flow_key)
            self.logger.info(f"New flow detected: {flow_key[:2]}... (total packets: {len(self.flow_buffers)})")
        
        # Add packet to buffer
        buffer = self.flow_buffers[flow_key]
        timestamp = time.time()
        completed = buffer.add_packet(msg.data, timestamp, src_ip)

        self.logger.debug(f"Flow buffer: {buffer.packet_count()}/{FLOW_SAMPLES} packets")
        
        if completed:
            # Enough packets collected — classify!
            self._classify_flow(datapath, flow_key, in_port, buffer)
    
    def _extract_flow_key(self, pkt, in_port):
        """
        Extract unique flow identifier from packet.
        Returns tuple (src_ip, dst_ip, src_port, dst_port, protocol)
        """
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
    
    def _classify_flow(self, datapath, flow_key, in_port, buffer):
        features = buffer.extract_features()
        
        if not features:
            # Fallback to default classification
            self.logger.warning(f"Insufficient features for {flow_key[:2]}, using default")
            self.flow_classifications[flow_key] = 'default'
            self._install_qos_flow(datapath, flow_key, in_port, 'default', None)
            return
        
        # Prepare API request
        api_payload = {
            'sizes': features['sizes'],
            'ipts': features['ipts'],
            'dirs': features['dirs']
        }
        
        self.logger.info(f"Classifying flow {flow_key[:2]}... ({buffer.packet_count()} packets collected)")
        self.logger.debug(f"Features: {api_payload}")
        
        # Call ML backend
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
                
                # Store classification
                self.flow_classifications[flow_key] = traffic_class
                
                # Install QoS policy
                self._install_qos_flow(datapath, flow_key, in_port, traffic_class, None)
            else:
                self.logger.warning(f"API error: {response.status_code}")
                self._install_default_flow(datapath, flow_key, in_port)
                
        except requests.exceptions.RequestException as e:
            self.logger.warning(f"ML backend unreachable: {e}")
            self.stats['api_failures'] += 1
            self._install_default_flow(datapath, flow_key, in_port)
        
        # Remove from buffers
        del self.flow_buffers[flow_key]
    
    def _install_qos_flow(self, datapath, flow_key, in_port, traffic_class, data):
        """
        Install OpenFlow rule with QoS policies.
        """
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        
        src_ip, dst_ip, src_port, dst_port, protocol, in_port_val = flow_key
        
        # Build match
        match = parser.OFPMatch(
            eth_type=0x0800,  # IPv4
            ip_proto=protocol,
            ipv4_src=src_ip,
            ipv4_dst=dst_ip,
        )
        
        if src_port and dst_port:
            if protocol == 6:  # TCP
                match.set_tcp_src(src_port)
                match.set_tcp_dst(dst_port)
            elif protocol == 17:  # UDP
                match.set_udp_src(src_port)
                match.set_udp_dst(dst_port)
        
        # Determine priority and meter based on traffic class
        priority = PRIORITY_MAP.get(traffic_class, 4)
        meter_id = METER_MAP.get(traffic_class, 2)
        
        # Apply actions: output to all ports (flooding for demo)
        actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
        
        # Instructions with meter for rate limiting
        instructions = [
            parser.OFPInstructionMeter(meter_id),
            parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)
        ]
        
        # Install flow with timeout (remove after inactivity)
        mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=instructions,
            idle_timeout=60,   # Remove after 60s inactivity
            hard_timeout=300,  # Remove after 5 minutes
            buffer_id=ofproto.OFP_NO_BUFFER,
        )
        
        datapath.send_msg(mod)
        
        self.stats['policies_applied'] += 1
        self.logger.info(f"Installed QoS flow: {traffic_class} (priority={priority}, meter={meter_id})")
    
    def _install_default_flow(self, datapath, flow_key, in_port):
        """
        Install default flow when ML backend is unavailable.
        """
        self.flow_classifications[flow_key] = 'default'
        self._install_qos_flow(datapath, flow_key, in_port, 'default', None)
    
    def _forward_normal(self, datapath, in_port, data):
        """
        Forward non-IP traffic normally.
        """
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
    

    # Maintenance
    
    def _cleanup_loop(self):
       #Background thread to clean up expired flow buffers
        while True:
            hub.sleep(1)
            
            expired = []
            for flow_key, buffer in self.flow_buffers.items():
                if buffer.is_expired():
                    expired.append(flow_key)
            
            for flow_key in expired:
                buffer = self.flow_buffers[flow_key]
                self.logger.warning(f"Flow {flow_key} expired with {len(buffer.packets)} packets")
                
                # Classify with what we have
                # (This would need datapath — simplified for now)
                del self.flow_buffers[flow_key]


    # Utility

    @set_ev_cls(ofp_event.EventOFPErrorMsg, MAIN_DISPATCHER)
    def error_handler(self, ev):
        """Log OpenFlow errors"""
        self.logger.error(f"OpenFlow error: {ev.msg}")
    
    def get_stats(self):
        """Return controller statistics"""
        return {
            'flows_classified': self.stats['flows_classified'],
            'policies_applied': self.stats['policies_applied'],
            'api_calls': self.stats['api_calls'],
            'api_failures': self.stats['api_failures'],
            'packets_captured': self.stats['packets_captured'],
            'active_buffers': len(self.flow_buffers),
            'classified_flows': len(self.flow_classifications),
        }

