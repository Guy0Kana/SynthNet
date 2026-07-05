#!/usr/bin/env python3
"""
SynthNet Tiny TC Server
Runs on each Mininet host as a lightweight HTTP server.
Receives tc commands from Ryu controller and applies them locally.

Usage (started automatically by campus_topo.py):
    python3 tiny_tc_server.py <port> <interface>
    e.g. python3 tiny_tc_server.py 9001 h1-eth0


import sys
import subprocess
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
import json

logging.basicConfig(
    level=logging.INFO,
    format='[TC-SERVER] %(message)s'
)
logger = logging.getLogger(__name__)

LINK_MBIT = 1000  # must match campus_topo.py link bw


def apply_tc(intf, rate_mbit, prio, ceil_mbit=LINK_MBIT):
    """Apply HTB tc rules to interface."""
    cmds = [
        # Clear existing rules
        f"tc qdisc del dev {intf} root 2>/dev/null || true",
        # Root HTB qdisc
        f"tc qdisc add dev {intf} root handle 1: htb default 10",
        # Root class — full link speed
        f"tc class add dev {intf} parent 1: classid 1:1 htb rate {LINK_MBIT}mbit",
        # Traffic class with guaranteed rate and ceiling
        f"tc class add dev {intf} parent 1:1 classid 1:10 "
        f"htb rate {rate_mbit}mbit ceil {ceil_mbit}mbit prio {prio}",
        # FIFO leaf qdisc
        f"tc qdisc add dev {intf} parent 1:10 handle 10: pfifo limit 1000",
        # Filter — all traffic to class
        f"tc filter add dev {intf} parent 1: protocol ip prio 1 "
        f"u32 match ip dst 0.0.0.0/0 flowid 1:10",
    ]

    for cmd in cmds:
        result = subprocess.run(
            cmd, shell=True,
            capture_output=True, text=True
        )
        if result.returncode != 0 and 'No such file' not in result.stderr \
                and 'Cannot find' not in result.stderr \
                and 'RTNETLINK' not in result.stderr:
            logger.warning(f"tc cmd warning: {cmd} → {result.stderr.strip()}")

    logger.info(f"Applied: {intf} rate={rate_mbit}mbit ceil={ceil_mbit}mbit prio={prio}")


class TCHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # suppress default HTTP logs — use our own logger

    def do_POST(self):
        if self.path != '/set_tc':
            self._respond(404, {'error': 'not found'})
            return

        try:
            length = int(self.headers.get('Content-Length', 0))
            body   = self.rfile.read(length)
            data   = json.loads(body)

            rate = int(data.get('rate', 50))
            prio = int(data.get('prio', 4))
            intf = data.get('intf', self.server.intf)
            ceil = int(data.get('ceil', LINK_MBIT))

            apply_tc(intf, rate, prio, ceil)

            self._respond(200, {
                'status': 'ok',
                'intf':   intf,
                'rate':   rate,
                'prio':   prio,
                'ceil':   ceil,
            })

        except Exception as e:
            logger.error(f"Error applying tc: {e}")
            self._respond(500, {'error': str(e)})

    def do_GET(self):
        if self.path == '/health':
            self._respond(200, {'status': 'ok', 'intf': self.server.intf})
        else:
            self._respond(404, {'error': 'not found'})

    def _respond(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(data))
        self.end_headers()
        self.wfile.write(data)


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 tiny_tc_server.py <port> <interface>")
        sys.exit(1)

    port = int(sys.argv[1])
    intf = sys.argv[2]

    server = HTTPServer(('0.0.0.0', port), TCHandler)
    server.intf = intf  # attach intf to server so handler can access it

    logger.info(f"Listening on port {port}, managing interface {intf}")
    server.serve_forever()


if __name__ == '__main__':
    main()
"""
