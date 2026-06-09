# Mininet Topology Scripts
This section details the scripts ensuring mininet's operability.

## Campus_topo.py
This script defines a Mininet network topology that emulates a campus network with six client hosts, each representing a different client type, and a central server as the traffic destination. The topology connects to an external Ryu SDN controller for OpenFlow-based QoS enforcement.

### Details
<pre>
Component       |  Count |   Details
OpenFlow Switch |	1    |   Managed by Ryu controller
Client Hosts    | 	6    |	 h1 through h6
Server Host	    |   1    |	 10.0.0.10 (traffic destination)
Link Bandwidth  |   1    |   Gbps	All links
Link Delay      | 	5    |   ms	All links
</pre>

### Traffic Type Mapping
<pre>
Host	IP Address	Traffic Type	        QoS Priority
h1  	  10.0.0.1	 VoIP	                  High
h2  	  10.0.0.2	 Video Conferencing   	High
h3    	10.0.0.3	 Web Browsing         	Medium
h4    	10.0.0.4	 File Transfer        	Low
h5   	  10.0.0.5	 P2P/Background	        Low
h6	    10.0.0.6	 Cloud/Email          	Medium
server	10.0.0.10	 Destination           	N/A
</pre>

### Usage
#### 1. Start the Ryu controller first.
In the absence of custom Ryu controller scripts, run the following code to provide standard switch capability.
```bash
ryu-manager ryu.app.simple_switch_13
```

#### 2. Start the topology script
```bash
sudo python3 campus_topo.py
```

Once using mininet, run ``` pingall ``` to test connectivity between devices.


## Iperf3_traffic.py
This script enables realistic, multi-flow traffic generation to test QoS policies such as prioritization, bandwidth allocation, and congestion management in an SDN environment.

### Features
- Multi-port Architecture: Each traffic profile uses a dedicated iperf3 server port (5201-5206), eliminating connection conflicts (first one to run hogs connection).

- Concurrent Execution: Uses threading to run all traffic types simultaneously (lambda for all hosts)

- JSON Logging: Parses iperf3 JSON output and logs key metrics (bandwidth, jitter, loss, retransmits)

- CSV Export: Saves results to logs/traffic_gen.csv for analysis (found within ~/SynthNet/mininet)

- Thread-Safe: Uses locks for safe concurrent log writing

- Host Resolution: Resolves host objects before thread execution to avoid namespace issues (_h(name) function)

### Available Custom Traffic Functions
#### Individual traffic functions
- run_voip(host, duration) : h1, 30 secs
  
- run_video(host, duration) : h2, 30 secs

- run_web(host, duration)	: h3. 30 secs

- run_file_transfer(host, duration)	: h4, 30 secs

- run_background(host, duration) : h5, 30 secs

- run_cloud(host, duration)	: h6. 30 secs

- run_dns(host, count)	

- run_ping(host, count)

#### Composite functions
- run_all_traffic(duration) :	Runs all 6 traffic types simultaneously from h1-h6
  
- run_voip_vs_web(duration) :	Priority test: VoIP (high) vs Web (low)
  
- run_stress_test(duration, streams) :	High load with parallel streams from multiple hosts
  
- stop_all_traffic() :	Kills all iperf3 processes on all hosts
  
- save_logs()	: Exports collected metrics to CSV

### Usage in Mininet CLI
```bash
py globals().update({'net':net, 'SERVER_HOST': 'server'})
py exec(open('iperf3_traffic.py').read(), globals())
server iperf3 -s -p 5201 &
server iperf3 -s -p 5202 &
# ... ports 5203-5206. However, running 'server iperf3 -s &' also satisfies all

py... commands
```





