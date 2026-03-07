#!/usr/bin/env python3
"""
JAM Player 2.0 - Chrony Peer Discovery Service

This service enables JAM Players to sync their clocks with each other,
providing resilience when some or all devices lose internet connectivity.

Connectivity Scenarios (up to 4 JAM Players per ScreenLayout):

1. ALL ONLINE: Each device syncs to NTP servers (stratum ~2). Peers are
   discovered but chrony prefers NTP due to lower stratum.

2. ALL OFFLINE: Devices peer with each other. One device (lowest stratum
   or first to lose NTP) becomes the reference. Others sync to it.
   The `local stratum 10` setting allows serving time from hardware clock.

3. MIXED (some online, some offline): Online devices sync to NTP (stratum 2).
   Offline devices peer with online ones and receive time at stratum 3-4.
   This effectively "bridges" internet time to offline devices.

In all cases, chrony automatically selects the best available time source
(lowest stratum = highest accuracy). This ensures all JAM Players in a
layout maintain synchronized clocks for content playback sync.

How it works:
1. Broadcasts presence via UDP multicast every 10 seconds
2. Discovers other JAM Players on the same network
3. Adds discovered devices as chrony peers via `chronyc add peer`
4. Removes peers that haven't been seen for 60 seconds
5. Chrony handles source selection automatically based on stratum/accuracy
"""

import os
import sys
import time
import json
import socket
import struct
import signal
import subprocess
import threading
from typing import Dict, Set
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from common.logging_config import setup_service_logging
from common.credentials import get_device_uuid

logger = setup_service_logging("jam-chrony-peering")

# Multicast configuration (same group as display sync, different port)
MULTICAST_GROUP = '239.255.42.1'
MULTICAST_PORT = 5743  # Different port from display sync (5742)
MULTICAST_TTL = 1  # Local network only

# Timing configuration
ANNOUNCE_INTERVAL_SEC = 10  # How often to announce presence
PEER_TIMEOUT_SEC = 60  # Remove peer if not seen for this long
CLEANUP_INTERVAL_SEC = 30  # How often to clean up stale peers


class ChronyPeeringService:
    """
    Discovers other JAM Players and configures chrony to peer with them.
    """

    def __init__(self):
        self.device_uuid = get_device_uuid()
        if not self.device_uuid:
            raise RuntimeError("Device UUID not found - device not provisioned")

        self.running = False
        self._send_socket = None
        self._recv_socket = None

        # Track discovered peers: {device_uuid: {'ip': str, 'last_seen': float}}
        self._peers: Dict[str, dict] = {}
        self._peers_lock = threading.Lock()

        # Track which peers we've added to chrony
        self._chrony_peers: Set[str] = set()  # Set of IP addresses

        # Get our own IP for filtering
        self._my_ips = self._get_local_ips()

        logger.info(f"ChronyPeeringService initialized for device {self.device_uuid[:8]}...")
        logger.info(f"Local IPs: {self._my_ips}")

    def _get_local_ips(self) -> Set[str]:
        """Get all local IP addresses to filter out our own announcements."""
        ips = set()
        try:
            # Get all network interfaces
            result = subprocess.run(
                ['hostname', '-I'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                ips = set(result.stdout.strip().split())
        except Exception as e:
            logger.warning(f"Failed to get local IPs: {e}")
        return ips

    def start(self):
        """Start the peering service."""
        logger.info("Starting Chrony Peering Service")
        self.running = True

        # Setup sockets
        self._setup_sockets()

        # Start threads
        self._announce_thread = threading.Thread(target=self._announce_loop, daemon=True)
        self._receive_thread = threading.Thread(target=self._receive_loop, daemon=True)
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)

        self._announce_thread.start()
        self._receive_thread.start()
        self._cleanup_thread.start()

        logger.info(f"Chrony peering active on {MULTICAST_GROUP}:{MULTICAST_PORT}")

    def stop(self):
        """Stop the peering service."""
        logger.info("Stopping Chrony Peering Service")
        self.running = False

        # Close sockets
        if self._send_socket:
            try:
                self._send_socket.close()
            except:
                pass

        if self._recv_socket:
            try:
                self._recv_socket.close()
            except:
                pass

        # Remove all peers from chrony
        self._remove_all_chrony_peers()

    def _setup_sockets(self):
        """Setup multicast sockets for send and receive."""
        try:
            # Send socket
            self._send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            self._send_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, MULTICAST_TTL)

            # Receive socket
            self._recv_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            self._recv_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._recv_socket.bind(('', MULTICAST_PORT))

            # Join multicast group
            mreq = struct.pack('4sl', socket.inet_aton(MULTICAST_GROUP), socket.INADDR_ANY)
            self._recv_socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

            # Set receive timeout
            self._recv_socket.settimeout(1.0)

            logger.info(f"Multicast sockets ready on {MULTICAST_GROUP}:{MULTICAST_PORT}")

        except Exception as e:
            logger.error(f"Failed to setup sockets: {e}")
            raise

    def _announce_loop(self):
        """Periodically announce our presence."""
        while self.running:
            try:
                self._send_announcement()
            except Exception as e:
                logger.warning(f"Error sending announcement: {e}")

            time.sleep(ANNOUNCE_INTERVAL_SEC)

    def _send_announcement(self):
        """Send a presence announcement."""
        if not self._send_socket:
            return

        announcement = {
            'type': 'jam-chrony-peer',
            'device_uuid': self.device_uuid,
            'timestamp': time.time(),
        }

        data = json.dumps(announcement).encode('utf-8')
        self._send_socket.sendto(data, (MULTICAST_GROUP, MULTICAST_PORT))

    def _receive_loop(self):
        """Receive announcements from other devices."""
        while self.running:
            try:
                data, addr = self._recv_socket.recvfrom(1024)
                self._handle_announcement(data, addr)
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    logger.warning(f"Error receiving: {e}")
                time.sleep(0.1)

    def _handle_announcement(self, data: bytes, addr: tuple):
        """Handle an announcement from another device."""
        try:
            announcement = json.loads(data.decode('utf-8'))
        except Exception as e:
            logger.debug(f"Invalid announcement from {addr}: {e}")
            return

        if announcement.get('type') != 'jam-chrony-peer':
            return

        device_uuid = announcement.get('device_uuid')
        if not device_uuid:
            return

        # Ignore our own announcements
        if device_uuid == self.device_uuid:
            return

        peer_ip = addr[0]

        # Also ignore if it's one of our own IPs (shouldn't happen, but safety check)
        if peer_ip in self._my_ips:
            return

        # Update peer info
        with self._peers_lock:
            is_new = device_uuid not in self._peers
            self._peers[device_uuid] = {
                'ip': peer_ip,
                'last_seen': time.time(),
            }

        if is_new:
            logger.info(f"Discovered peer: {device_uuid[:8]}... at {peer_ip}")
            self._add_chrony_peer(peer_ip)

    def _cleanup_loop(self):
        """Periodically clean up stale peers."""
        while self.running:
            time.sleep(CLEANUP_INTERVAL_SEC)

            try:
                self._cleanup_stale_peers()
            except Exception as e:
                logger.warning(f"Error during cleanup: {e}")

    def _cleanup_stale_peers(self):
        """Remove peers that haven't been seen recently."""
        now = time.time()
        stale_uuids = []

        with self._peers_lock:
            for device_uuid, info in list(self._peers.items()):
                if now - info['last_seen'] > PEER_TIMEOUT_SEC:
                    stale_uuids.append(device_uuid)
                    peer_ip = info['ip']
                    logger.info(f"Peer timeout: {device_uuid[:8]}... at {peer_ip}")
                    self._remove_chrony_peer(peer_ip)
                    del self._peers[device_uuid]

    def _add_chrony_peer(self, ip: str):
        """Add a peer to chrony."""
        if ip in self._chrony_peers:
            return

        try:
            # Add peer to chrony
            # The 'iburst' option speeds up initial sync
            # The 'prefer' option isn't used - we let chrony decide
            result = subprocess.run(
                ['chronyc', 'add', 'peer', ip, 'iburst'],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode == 0:
                self._chrony_peers.add(ip)
                logger.info(f"Added chrony peer: {ip}")
            else:
                # "Source already present" is not an error
                if 'already present' in result.stderr.lower() or 'already present' in result.stdout.lower():
                    self._chrony_peers.add(ip)
                    logger.debug(f"Chrony peer already exists: {ip}")
                else:
                    logger.warning(f"Failed to add chrony peer {ip}: {result.stderr or result.stdout}")

        except subprocess.TimeoutExpired:
            logger.warning(f"Timeout adding chrony peer: {ip}")
        except Exception as e:
            logger.warning(f"Error adding chrony peer {ip}: {e}")

    def _remove_chrony_peer(self, ip: str):
        """Remove a peer from chrony."""
        if ip not in self._chrony_peers:
            return

        try:
            result = subprocess.run(
                ['chronyc', 'delete', ip],
                capture_output=True,
                text=True,
                timeout=10
            )

            self._chrony_peers.discard(ip)

            if result.returncode == 0:
                logger.info(f"Removed chrony peer: {ip}")
            else:
                logger.debug(f"Chrony peer removal note: {result.stderr or result.stdout}")

        except Exception as e:
            logger.warning(f"Error removing chrony peer {ip}: {e}")
            self._chrony_peers.discard(ip)

    def _remove_all_chrony_peers(self):
        """Remove all peers from chrony on shutdown."""
        for ip in list(self._chrony_peers):
            self._remove_chrony_peer(ip)

    def get_peer_count(self) -> int:
        """Get the number of active peers."""
        with self._peers_lock:
            return len(self._peers)


def main():
    """Main entry point."""
    logger.info("=" * 60)
    logger.info("JAM Chrony Peering Service Starting")
    logger.info("=" * 60)

    service = ChronyPeeringService()

    # Signal handlers for graceful shutdown
    def shutdown(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        service.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Start the service
    service.start()

    # Keep running
    try:
        while True:
            time.sleep(60)
            peer_count = service.get_peer_count()
            if peer_count > 0:
                logger.info(f"Active chrony peers: {peer_count}")
    except KeyboardInterrupt:
        pass

    service.stop()


if __name__ == '__main__':
    main()
