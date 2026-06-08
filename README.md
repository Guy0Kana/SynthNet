# SynthNet
ML-SDN system for bandwidth optimization through QoS policy enforcement and traffic prioritization in campus networks.

## Description
Campus networks in the modern age operate at the scale of their enterprise counterparts, but with legacy infrastructure that struggle to support modern digital services these networks host. The primary issue this deprecated technology presents is the inefficient bandwidth usage, with bandwidth greedy applications hogging the link and worsening the experience for critical services these networks use (such as VoIP and Video calls).
To deal with this problem, I intend to deploy a Machine Learning and Software Defined Networking system that classifies network traffic, maps the classified traffic into priority levels and uses these priority levels to dynamically assign bandwidth and enforce QoS policies within the system, ensuring critical services remain available even in times of peak use.

## Project Structure
├── diagrams/ # System architecture and network topology diagrams
├── ryu/ # Ryu SDN controller python code
├── mininet/ # Mininet topology and traffic generation scripts
├── backend/ # FastAPI + 30pktTCNET model + SQLite/CSV logging
│ ├── model/ # Pre-trained 30pktTCNET model for inference
│ ├── database/ # Inference logs (SQLite/CSV)
│ └── app.py # REST endpoint for classification
├── web-dashboard/ # Traffic classification visualization UI
└── docs/ # Additional documentation
