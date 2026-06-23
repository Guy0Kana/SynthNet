# Overview
This document covers common OpenFlow errors encountered during QoS flow installation in Ryu-based SDN controllers, along with their root causes and fixes.

## Error Type 1: Meter Installation Failure
Error Signature
text
```OFPErrorMsg(code=1, type=12)
Type: OFPET_METER_MOD_FAILED (12)
```

Code: OFPMMFC_INVALID_METER (1)

### Description
The controller attempted to install a meter entry that OVS rejected. Meters are used for rate limiting and QoS, but may fail if:

The meter ID is already in use

The meter bands are invalid

OVS was compiled without meter support

The meter configuration exceeds hardware limits

### Impact
QoS flows with meter instructions are rejected

Traffic shaping/rate limiting fails

Packets continue without bandwidth control

### Solution
#### Option A: Disable Meters (Recommended)

```python
# In switch_features_handler()
# ❌ Remove or comment out:
# self._install_meters(datapath)

# ✅ Only install base flows:
self._install_default_flow(datapath)
```

#### Option B: Fix Meter Configuration

```python
# Ensure valid meter bands
bands = [parser.OFPMeterBandDrop(rate=rate, burst_size=burst)]
meter_mod = parser.OFPMeterMod(
    datapath=datapath,
    command=ofproto.OFPMC_ADD,
    flags=ofproto.OFPMF_KBPS,
    meter_id=meter_id,
    bands=bands
)
```

## Error Type 2: Invalid Output Action
Error Signature
text
```OFPErrorMsg(code=9, type=4)
Type: OFPET_BAD_ACTION (4)
```

Code: OFPBAC_BAD_OUT_PORT (9)

### Description
OVS rejected a flow rule containing an invalid output port. The most common cause is using OFPP_FLOOD for unicast flows, which OVS treats as an invalid action in certain contexts.

### Impact
Flow installation fails

Packets are dropped or flooded incorrectly

Traffic doesn't reach destination hosts

Idle timeouts occur as no flow rule exists for subsequent packets

### Solution
Replace OFPP_FLOOD with OFPP_NORMAL:

```python
# ❌ Causes error:
actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]

# ✅ Correct approach:
actions = [parser.OFPActionOutput(ofproto.OFPP_NORMAL)]
```

**Why OFPP_NORMAL?**

Uses OVS's normal L2 learning/forwarding

More appropriate for installed flow rules

Supports both unicast and multicast traffic

Avoids broadcast storms

**Complete Fix Example**
```
python
def _install_qos_flow(self, datapath, priority, match, actions=None):
    parser = datapath.ofproto_parser
    ofproto = datapath.ofproto
    
    # Use normal forwarding instead of flood
    if actions is None:
        actions = [parser.OFPActionOutput(ofproto.OFPP_NORMAL)]
    
    # No meter instructions (if disabled)
    instructions = [
        parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)
    ]
    
    flow_mod = parser.OFPFlowMod(
        datapath=datapath,
        priority=priority,
        match=match,
        instructions=instructions,
        idle_timeout=0,
        hard_timeout=0
    )
    datapath.send_msg(flow_mod)
```

## Error Type 3: Flow Buffer Timeout
Error Signature
text
```iperf3: error - idle timeout for receiving data```

### Description
Traffic stops flowing after initial connection establishment, causing iperf3 to timeout waiting for data. This is typically a symptom of:

Failed flow rule installation (see Error Type 2)

Flow rules expiring before traffic completes

Missing return path flows

### Impact
Connections establish but no data transfers

Throughput drops to zero

Tests fail with idle timeout errors

### Root Cause Chain
text
1. Packet arrives (first few packets get through)
2. Controller attempts to install QoS flow
3. Flow installation fails (e.g., BAD_OUT_PORT)
4. Subsequent packets have no matching rule
5. Packets are dropped
6. iperf3 sees no data → idle timeout

### Solution
Fix underlying OpenFlow errors (Error Types 1 & 2)

Ensure bidirectional flow rules exist

Verify output ports are valid

## Error Type 4: Classification Timeout
Error Signature
text
```Flow buffer timeout: Removes buffer without classification```

### Description
A flow exceeded the buffer window before ML classification could complete, causing the buffer to be discarded without priority assignment.

### Impact
Default priority (4) is applied

Traffic may not receive proper QoS treatment

Classification accuracy decreases

### Solution
```python
# Increase buffer timeout if needed
buffer_timeout = 5  # seconds (default)

# Or ensure ML backend is responsive
# Check API connectivity and model load time
```
