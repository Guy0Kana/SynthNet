# Ryu SDN Controller Code
```qos_controller.py```

## Overview
The Ryu SDN controller provides ML-based QoS enforcement for campus networks. It captures the first 30 packets of each new flow, extracts PPI features (packet sizes, inter-packet times, directions) and 17 flow statistics, sends them to the FastAPI ML backend for classification, and installs OpenFlow rules with appropriate QoS priorities.

## Flow Classification Pipeline
### 1. Packet Capture
- Intercepts first 30 packets of each new flow
- Extracts packet size, timestamp, and direction
- Captures TCP flags for flowstats

### 2. Feature Extraction
PPI Features (3 × 30):

- sizes — Packet sizes in bytes

- ipts — Inter-packet times in microseconds

- dirs — Direction (1 = forward, -1 = backward)

### 3. ML Classification
- Sends PPI + flowstats to FastAPI endpoint (/classify)

- Receives campus category and QoS priority

- Falls back to default if API unreachable

## Key Components
### FlowBuffer Class
Buffers packets and computes flowstats:

#### Methods
- add_packet():	Adds packet with direction and TCP flags
- get_flowstats():	Returns 17 flow statistics
- extract_ppi_features():	Returns PPI sequences (sizes, ipts, dirs)
- is_expired():	Checks if buffer exceeded timeout (5s)

### QoSRyuController Class
Main controller application:

#### Methods
- switch_features_handler():	Initializes switch with default flows
- packet_in_handler():	Processes incoming packets
- _extract_flow_key():	Extracts 5-tuple from packet
- _classify_flow():	Sends features to FastAPI
- _install_qos_flow():	Installs OpenFlow rule with priority
- _install_meters():	Installs rate-limiting meters (if supported)

## Dependencies
- ryu ≥ 4.34

- requests — HTTP client

- eventlet — Cooperative threading

- python 3.9+

## Error Handling
<pre>
| Scenario                     | Fallback Action                                        |
|------------------------------|--------------------------------------------------------|
| ML backend unreachable       | Installs default priority (4)                          |
| Insufficient packet features | Installs default priority (4)                          |
| Flow buffer timeout          | Removes buffer without classification                  |
</pre>

## Meter Configuration
<pre>
| Meter ID | Rate      | Burst   | Priority Classes          |
|----------|-----------|---------|---------------------------|
| 1        | 1 Gbps    | 100 KB  | voip, cloud_email         |
| 2        | 500 Mbps  | 50 KB   | dns, http                 |
| 3        | 100 Mbps  | 10 KB   | video, ftp                |
| 4        | 50 Mbps   | 5 KB    | background, p2p           |
</pre>

### Metering Logic
- Meter 1 (highest rate): Handles real-time and critical services (`voip`, `cloud_email`)
- Meter 2 (medium-high): Manages infrastructure and web traffic (`dns`, `http`)
- Meter 3 (medium-low): Covers bulk media and file transfers (`video`, `ftp`)
- Meter 4 (lowest rate): Best-effort and background traffic (`background`, `p2p`)

### Limitations
- Flooding used for demonstration (not optimal)

- Meters may fail if OVS not properly configured

- Classification requires first 30 packets (introduces initial delay)

## Installation
```bash
# Activate Ryu environment
source ~/ryu-env/bin/activate

# Navigate to Ryu directory
cd ~/SynthNet/ryu

# Start controller
ryu-manager qos_controller.py
```


