# Documentation: Complete Setup Guide

This guide walks users through setting up all required software for the SynthNet SDN QoS project. Before installation, ensure your system meets the following thresholds:

<pre>
OS: Ubuntu 20 or higher (preferably Ubuntu 24.04 LTS)
RAM: 8 GB
CPU Cores: 4
Storage: 60 GB
Internet: Required
</pre>

## Installation Steps

### 1. Update your system
Start with a fresh package list and upgraded system:

```bash
sudo apt update && sudo apt upgrade -y
```

### 2. Install Base Tools
Essential utilities required in the project:
```bash
sudo apt install -y curl wget python3 python3-pip python3-venv net-tools build-essential
```
curl/wget - downloading files and testing API endpoints
python3 - python interpreter
python3-pip - installs python libraries
python3.venv - creates isolated python environments
net-tools - netstat for debugging
build-essentials - compiles python packages

### 3. Install Mininet and Open vSWitch
Mininet emulates the network topology. Open vSWitch acts as the OpenFLow switch

```bash
sudo apt install -y mininet openvswitch-switch
```
Verify installation using 
```bash
sudo mn --version && sudo  ovs-vsctl show
```

### 4. Install Traffic Generation Tools
iperf3 is the preferred utility for generating network traffic for testing QoS, and can also measure bandwidth between two hosts.

```bash
sudo apt install -y iperf3
```

Verify using:

```bash
iperf3 --version
```

### 5. Set up Ryu Controller Env
Ryu is the SDN controller that installs QoS rules on the OpenFlow switch.
It runs on Python 3.9 due to its dependency on an older version of setuptools that doesn't work with Python 3.12.

```bash
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt update
sudo apt install -y python3.9 python3.9-venv python3.9-distutils
python3.9 -m venv ~/ryu-env
pip install setuptools==58.0.0
pip install ryu eventlet==0.30.2
```

Verify using:
```bash
ryu-manager --version
```

### 6. Set up ML Backend Env
This environment runs the FastAPI server and the 30pktTCNET machine learning model.
It uses modern packages thus must be kept separate from Ryu.

```bash
python3.12 -m venv ~/ml-env
source ~/ml-env/bin/activate
pip install torch fastapi uvicorn requests cesnet-models numpy pandas
```

### 7. Starting up virtual env
Run these commands in two separate terminals to get the venv up and running

```bash
source ~/ryu-env/bin/activate
```

```bash
source ~/ml-env/bin/activate
```



