#!/usr/bin/env python3
"""
JAM BLE Provisioning Service

This service allows users to configure their JAM Player's WiFi connection
via Bluetooth Low Energy (BLE) from the JAM Setup mobile app.

=== BLE GATT Overview ===

BLE uses a protocol called GATT (Generic Attribute Profile) for data exchange:

1. ADVERTISEMENT: The device broadcasts its presence so phones can discover it.
   - Our device advertises as "JAM-PLAYER-XXXXX" (last 5 chars of device UUID)
   - The mobile app scans for devices with this naming pattern

2. GATT APPLICATION: A container for all our BLE services.
   - Registered with BlueZ (Linux Bluetooth stack) via D-Bus

3. SERVICE: A logical grouping of related functionality.
   - Identified by a 128-bit UUID (we use custom UUIDs)
   - Our "WiFi Provisioning Service" contains all WiFi-related characteristics

4. CHARACTERISTIC: A single piece of data or action within a service.
   - Each has a UUID and flags (read, write, notify)
   - READ: Mobile app requests data from the Pi (e.g., list of WiFi networks)
   - WRITE: Mobile app sends data to the Pi (e.g., WiFi credentials)
   - NOTIFY: Pi pushes updates to the mobile app (e.g., connection status changes)

=== Our Service Structure ===

JAM-PLAYER-XXXXX (Advertisement)
└── WiFi Provisioning Service (UUID: 12345678-1234-5678-1234-56789abcdef0)
    ├── WiFi Networks (UUID: ...-def1) [READ]
    │   └── Returns JSON: [{"ssid": "MyNetwork", "signal": "75", "security": "WPA2"}]
    ├── WiFi Credentials (UUID: ...-def2) [WRITE]
    │   └── Accepts JSON: {"ssid": "MyNetwork", "password": "secret123"}
    ├── Connection Status (UUID: ...-def3) [READ, NOTIFY]
    │   └── Returns JSON: {"status": "connected", "network": "MyNetwork", "ip": "192.168.1.50"}
    └── Device Info (UUID: ...-def4) [READ]
        └── Returns JSON: {"deviceUuid": "019beb00-...", "version": "2.0"}

=== BlueZ and D-Bus ===

BlueZ is the official Linux Bluetooth stack. We communicate with it via D-Bus,
which is Linux's inter-process communication (IPC) system. Our service:
1. Connects to the system D-Bus
2. Registers our GATT application with BlueZ
3. Registers our advertisement with BlueZ
4. BlueZ handles all the low-level Bluetooth communication
5. When a phone connects and reads/writes, BlueZ calls our Python methods

=== Dependencies ===

- dbus-python: Python bindings for D-Bus
- PyGObject (gi.repository.GLib): Event loop for D-Bus communication
- BlueZ: Must be installed and running (apt install bluez)
"""

import sys
import json
import threading
import time
from pathlib import Path
from typing import Optional

# Add services directory to path for common module imports
sys.path.insert(0, str(Path(__file__).parent))

# D-Bus is Linux's inter-process communication system.
# BlueZ (Bluetooth stack) exposes its API via D-Bus.
import dbus
import dbus.exceptions
import dbus.mainloop.glib
import dbus.service

# GLib provides the main event loop that D-Bus needs to receive events
from gi.repository import GLib

from common.credentials import (
    get_device_uuid,
    get_device_uuid_short,
    get_jp_image_id,
    get_api_signing_public_key,
    get_ssh_public_key,
    is_device_announced,
    is_device_registered,
    set_device_registered,
    set_device_announced,
)
from common.api import api_request, get_api_base_url
from common.network import (
    get_available_wifi_networks,
    connect_to_wifi,
    get_current_connection_info,
    trigger_wifi_scan,
)
from common.paths import INTERNET_VERIFIED_FLAG
from common.network import check_internet_connectivity

# ============================================================================
# Logging Configuration
# ============================================================================

from common.logging_config import setup_service_logging, log_service_start

logger = setup_service_logging('jam-ble-provisioning')

# ============================================================================
# systemd Integration
# ============================================================================

from common.system import get_systemd_notifier, setup_signal_handlers, setup_glib_watchdog

# SystemdNotifier lets us tell systemd:
# - READY=1: Service has started successfully
# - WATCHDOG=1: Service is still alive (must send periodically)
# - STATUS=...: Human-readable status shown in `systemctl status`
sd_notifier = get_systemd_notifier()

# ============================================================================
# BlueZ D-Bus Interface Names
# ============================================================================

# These are the D-Bus interface names that BlueZ uses.
# Think of them as "API endpoints" that BlueZ exposes.

BLUEZ_SERVICE_NAME = 'org.bluez'  # The BlueZ service on D-Bus

# Interface for registering GATT applications (our services/characteristics)
GATT_MANAGER_IFACE = 'org.bluez.GattManager1'

# Interface for registering BLE advertisements (how phones discover us)
LE_ADVERTISING_MANAGER_IFACE = 'org.bluez.LEAdvertisingManager1'

# Standard D-Bus interfaces
DBUS_OM_IFACE = 'org.freedesktop.DBus.ObjectManager'  # List managed objects
DBUS_PROP_IFACE = 'org.freedesktop.DBus.Properties'   # Get/set properties

# BlueZ GATT interfaces - we implement these to create services/characteristics
GATT_SERVICE_IFACE = 'org.bluez.GattService1'
GATT_CHRC_IFACE = 'org.bluez.GattCharacteristic1'
LE_ADVERTISEMENT_IFACE = 'org.bluez.LEAdvertisement1'

# ============================================================================
# Custom UUIDs for JAM Provisioning
# ============================================================================

# BLE uses UUIDs to identify services and characteristics.
# Standard Bluetooth services have assigned 16-bit UUIDs (like 0x180D = Heart Rate).
# Custom services use full 128-bit UUIDs. We made these up - they just need to
# be unique and consistent between the Pi and mobile app.
#
# The mobile app will:
# 1. Scan for BLE devices advertising JAM_SERVICE_UUID
# 2. Connect to matching devices
# 3. Read/write characteristics using these UUIDs

JAM_SERVICE_UUID = '12345678-1234-5678-1234-56789abcdef0'       # Main service
WIFI_NETWORKS_UUID = '12345678-1234-5678-1234-56789abcdef1'     # Read available networks
WIFI_CREDENTIALS_UUID = '12345678-1234-5678-1234-56789abcdef2'  # Write SSID + password
CONNECTION_STATUS_UUID = '12345678-1234-5678-1234-56789abcdef3' # Read/notify status
DEVICE_INFO_UUID = '12345678-1234-5678-1234-56789abcdef4'       # Read device info
PROVISION_CONFIRM_UUID = '12345678-1234-5678-1234-56789abcdef5' # Write provisioning confirmation

# ============================================================================
# systemd Watchdog Configuration
# ============================================================================

# How often to ping the systemd watchdog (in seconds).
# The systemd unit file will have WatchdogSec set to 2-3x this value.
# If we stop pinging, systemd will restart our service automatically.
# This is DIFFERENT from the hardware watchdog that reboots the whole Pi.
WATCHDOG_INTERVAL = 30

# ============================================================================
# D-Bus Exception Classes
# ============================================================================

# These exceptions are returned to BLE clients when operations fail.
# They map to standard D-Bus/BlueZ error codes.


class InvalidArgsException(dbus.exceptions.DBusException):
    """Raised when a D-Bus method receives invalid arguments."""
    _dbus_error_name = 'org.freedesktop.DBus.Error.InvalidArgs'


class NotSupportedException(dbus.exceptions.DBusException):
    """Raised when an operation is not supported (e.g., write to read-only)."""
    _dbus_error_name = 'org.bluez.Error.NotSupported'


class NotPermittedException(dbus.exceptions.DBusException):
    """Raised when an operation is not permitted."""
    _dbus_error_name = 'org.bluez.Error.NotPermitted'


# ============================================================================
# BLE Advertisement
# ============================================================================

class Advertisement(dbus.service.Object):
    """
    BLE Advertisement - makes the device discoverable to phones.

    When a phone scans for Bluetooth devices, it sees advertisements.
    Our advertisement contains:
    - LocalName: "JAM-PLAYER-XXXXX" (visible in phone's BLE scanner)
    - ServiceUUIDs: Which services we offer (so the mobile app can filter)
    - ManufacturerData: Status flags so mobile app knows device state before connecting
    - Type: "peripheral" (we're a device that phones connect TO)

    Manufacturer Data Format:
    - Manufacturer ID: 0xFFFF (reserved for testing/internal use)
    - Byte 0: Status flags
        - Bit 0: isConnected (has internet)
        - Bit 1: isAnnounced (announced to backend)
        - Bit 2: isRegistered (fully registered)
    - Byte 1: Protocol version (0x01)

    The advertisement runs continuously until we unregister it.
    Multiple phones can see the advertisement simultaneously.
    """

    PATH_BASE = '/org/bluez/jam/advertisement'

    # Manufacturer ID 0xFFFF is reserved for internal/development use
    # For production, JAM should register with Bluetooth SIG for a real ID
    MANUFACTURER_ID = 0xFFFF
    PROTOCOL_VERSION = 0x01

    def __init__(self, bus, index: int, device_name: str, status_flags: int):
        """
        Create a new advertisement.

        Args:
            bus: D-Bus system bus connection
            index: Unique index for this advertisement (we only have one, so 0)
            device_name: Name to advertise (e.g., "JAM-PLAYER-A7F3D")
            status_flags: Byte with status bits (isConnected, isAnnounced, isRegistered)
        """
        self.path = f"{self.PATH_BASE}{index}"
        self.bus = bus
        self.ad_type = 'peripheral'  # We're a peripheral device, phones are "central"
        self.local_name = device_name
        self.service_uuids = [JAM_SERVICE_UUID]  # Advertise our service UUID
        self.include_tx_power = True  # Include signal strength info
        self.status_flags = status_flags
        dbus.service.Object.__init__(self, bus, self.path)

    def get_properties(self):
        """Return advertisement properties for BlueZ."""
        # Manufacturer data: [status_flags, protocol_version]
        manufacturer_data = dbus.Array([
            dbus.Byte(self.status_flags),
            dbus.Byte(self.PROTOCOL_VERSION),
        ], signature='y')

        return {
            LE_ADVERTISEMENT_IFACE: {
                'Type': self.ad_type,
                'ServiceUUIDs': dbus.Array(self.service_uuids, signature='s'),
                'LocalName': dbus.String(self.local_name),
                'IncludeTxPower': dbus.Boolean(self.include_tx_power),
                'ManufacturerData': dbus.Dictionary(
                    {dbus.UInt16(self.MANUFACTURER_ID): manufacturer_data},
                    signature='qv'
                ),
            }
        }

    def get_path(self):
        """Return the D-Bus object path for this advertisement."""
        return dbus.ObjectPath(self.path)

    @dbus.service.method(DBUS_PROP_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        """D-Bus method: Return all properties for an interface."""
        if interface != LE_ADVERTISEMENT_IFACE:
            raise InvalidArgsException()
        return self.get_properties()[LE_ADVERTISEMENT_IFACE]

    @dbus.service.method(LE_ADVERTISEMENT_IFACE, in_signature='', out_signature='')
    def Release(self):
        """D-Bus method: Called by BlueZ when advertisement is unregistered."""
        logger.info(f"Advertisement released: {self.path}")


# ============================================================================
# GATT Application
# ============================================================================

class Application(dbus.service.Object):
    """
    GATT Application - container for all our BLE services.

    BlueZ requires us to register an "application" that contains our services.
    This is just a container - the actual functionality is in the Service
    and Characteristic classes.

    When BlueZ calls GetManagedObjects(), we return a dict describing
    all our services and characteristics. BlueZ uses this to know what
    to expose to connected phones.
    """

    def __init__(self, bus):
        self.path = '/org/bluez/jam'
        self.services = []
        dbus.service.Object.__init__(self, bus, self.path)

    def get_path(self):
        """Return the D-Bus object path for this application."""
        return dbus.ObjectPath(self.path)

    def add_service(self, service):
        """Add a GATT service to this application."""
        self.services.append(service)

    @dbus.service.method(DBUS_OM_IFACE, out_signature='a{oa{sa{sv}}}')
    def GetManagedObjects(self):
        """
        D-Bus method: Return all managed objects (services and characteristics).

        BlueZ calls this to discover what services/characteristics we provide.
        Returns a nested dict: {object_path: {interface: {property: value}}}
        """
        response = {}
        for service in self.services:
            response[service.get_path()] = service.get_properties()
            for chrc in service.get_characteristics():
                response[chrc.get_path()] = chrc.get_properties()
        return response


# ============================================================================
# GATT Service Base Class
# ============================================================================

class Service(dbus.service.Object):
    """
    Base class for a GATT Service.

    A service is a logical grouping of related characteristics.
    For example, a "Heart Rate Service" might contain characteristics
    for heart rate measurement, body sensor location, etc.

    Our "WiFi Provisioning Service" contains characteristics for
    scanning networks, submitting credentials, and checking status.
    """

    PATH_BASE = '/org/bluez/jam/service'

    def __init__(self, bus, index: int, uuid: str, primary: bool):
        """
        Create a new GATT service.

        Args:
            bus: D-Bus system bus connection
            index: Unique index for this service
            uuid: 128-bit UUID identifying this service
            primary: True if this is a primary service (vs. secondary/included)
        """
        self.path = f"{self.PATH_BASE}{index}"
        self.bus = bus
        self.uuid = uuid
        self.primary = primary
        self.characteristics = []
        dbus.service.Object.__init__(self, bus, self.path)

    def get_properties(self):
        """Return service properties for BlueZ."""
        return {
            GATT_SERVICE_IFACE: {
                'UUID': self.uuid,
                'Primary': self.primary,
                'Characteristics': dbus.Array(
                    [c.get_path() for c in self.characteristics],
                    signature='o'
                )
            }
        }

    def get_path(self):
        """Return the D-Bus object path for this service."""
        return dbus.ObjectPath(self.path)

    def add_characteristic(self, characteristic):
        """Add a characteristic to this service."""
        self.characteristics.append(characteristic)

    def get_characteristics(self):
        """Return all characteristics in this service."""
        return self.characteristics


# ============================================================================
# GATT Characteristic Base Class
# ============================================================================

class Characteristic(dbus.service.Object):
    """
    Base class for a GATT Characteristic.

    A characteristic is a single piece of data or action within a service.
    Each characteristic has:
    - UUID: Unique identifier
    - Flags: What operations are allowed (read, write, notify, etc.)
    - Value: The actual data (for read/write characteristics)

    Common flags:
    - 'read': Client can read the value
    - 'write': Client can write a new value
    - 'notify': Server can push updates to the client
    - 'write-without-response': Client can write without waiting for confirmation
    """

    def __init__(self, bus, index: int, uuid: str, flags: list, service):
        """
        Create a new GATT characteristic.

        Args:
            bus: D-Bus system bus connection
            index: Unique index within the parent service
            uuid: 128-bit UUID identifying this characteristic
            flags: List of allowed operations ('read', 'write', 'notify')
            service: Parent service this characteristic belongs to
        """
        self.path = f"{service.path}/char{index}"
        self.bus = bus
        self.uuid = uuid
        self.service = service
        self.flags = flags
        self.value = []
        dbus.service.Object.__init__(self, bus, self.path)

    def get_properties(self):
        """Return characteristic properties for BlueZ."""
        return {
            GATT_CHRC_IFACE: {
                'Service': self.service.get_path(),
                'UUID': self.uuid,
                'Flags': self.flags,
            }
        }

    def get_path(self):
        """Return the D-Bus object path for this characteristic."""
        return dbus.ObjectPath(self.path)

    @dbus.service.method(DBUS_PROP_IFACE, in_signature='s', out_signature='a{sv}')
    def GetAll(self, interface):
        """D-Bus method: Return all properties for an interface."""
        if interface != GATT_CHRC_IFACE:
            raise InvalidArgsException()
        return self.get_properties()[GATT_CHRC_IFACE]

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='a{sv}', out_signature='ay')
    def ReadValue(self, options):
        """
        D-Bus method: Read the characteristic value.

        Called when a BLE client reads this characteristic.
        Subclasses should override this to return actual data.

        Args:
            options: Dict of read options (offset, device, etc.)

        Returns:
            Array of bytes (the characteristic value)
        """
        raise NotSupportedException()

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='aya{sv}')
    def WriteValue(self, value, options):
        """
        D-Bus method: Write a new characteristic value.

        Called when a BLE client writes to this characteristic.
        Subclasses should override this to handle the written data.

        Args:
            value: Array of bytes written by the client
            options: Dict of write options (offset, device, etc.)
        """
        raise NotSupportedException()

    @dbus.service.signal(DBUS_PROP_IFACE, signature='sa{sv}as')
    def PropertiesChanged(self, interface, changed, invalidated):
        """
        D-Bus signal: Notify clients that properties changed.

        Used by 'notify' characteristics to push updates to connected clients.
        """
        pass


# ============================================================================
# WiFi Networks Characteristic (READ)
# ============================================================================

class WiFiNetworksCharacteristic(Characteristic):
    """
    Characteristic to read available WiFi networks with notification-based chunking.

    Enterprise-grade BLE data transfer for payloads exceeding MTU:

    Protocol:
    1. Client subscribes to notifications (StartNotify)
    2. Client reads characteristic to trigger data transfer
    3. Server sends data in chunked notifications
    4. Each notification: [seq_num: 1 byte][flags: 1 byte][payload: up to 498 bytes]
       - seq_num: 0-255, wraps around for ordering
       - flags: 0x01 = last chunk, 0x00 = more chunks follow
    5. Client reassembles chunks in order, decodes JSON when complete

    Response format (JSON array):
    [{"ssid": "Network", "signal_strength": -45, "is_secured": true, "security_type": "WPA2"}, ...]
    """

    CHUNK_SIZE = 498  # 512 MTU - 2 byte header - ~12 bytes BLE overhead

    def __init__(self, bus, index: int, service):
        super().__init__(bus, index, WIFI_NETWORKS_UUID, ['read', 'notify'], service)
        self._notifying = False

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='a{sv}', out_signature='ay')
    def ReadValue(self, options):
        """
        Reading this characteristic triggers sending WiFi networks via notifications.
        Returns immediately with status; actual data comes via notifications.
        """
        logger.info("WiFi networks read requested - sending via notifications")

        if not self._notifying:
            # If notifications not enabled, return error message
            logger.warning("Client read WiFi networks but notifications not enabled")
            error = json.dumps({"error": "Subscribe to notifications first"})
            return dbus.Array([dbus.Byte(b) for b in error.encode('utf-8')], signature='y')

        # Trigger async notification sending
        GLib.idle_add(self._send_networks_chunked)

        # Return acknowledgment
        return dbus.Array([dbus.Byte(ord('O')), dbus.Byte(ord('K'))], signature='y')

    def _send_networks_chunked(self):
        """Send WiFi networks data in chunked notifications."""
        try:
            networks = get_available_wifi_networks()
            # Use standard field names matching iOS model
            formatted = []
            for net in networks:
                formatted.append({
                    'ssid': net.get('ssid', ''),
                    'signal_strength': net.get('signal_strength', -90),
                    'is_secured': net.get('is_secured', False),
                    'security_type': net.get('security_type', None)
                })

            data = json.dumps(formatted, separators=(',', ':')).encode('utf-8')
            total_size = len(data)
            logger.info(f"Sending {len(formatted)} WiFi networks ({total_size} bytes) in chunks")

            # Split into chunks and send as notifications
            seq_num = 0
            offset = 0

            while offset < total_size:
                chunk_end = min(offset + self.CHUNK_SIZE, total_size)
                chunk_data = data[offset:chunk_end]
                is_last = chunk_end >= total_size

                # Build packet: [seq_num][flags][data]
                flags = 0x01 if is_last else 0x00
                packet = bytes([seq_num, flags]) + chunk_data

                logger.debug(f"Sending chunk {seq_num}: {len(chunk_data)} bytes, is_last={is_last}")

                # Send notification
                self.PropertiesChanged(
                    GATT_CHRC_IFACE,
                    {'Value': dbus.Array([dbus.Byte(b) for b in packet], signature='y')},
                    []
                )

                seq_num = (seq_num + 1) % 256
                offset = chunk_end

                # Small delay between chunks to prevent overwhelming the BLE stack
                if not is_last:
                    time.sleep(0.05)  # 50ms between chunks

            logger.info(f"Finished sending {seq_num} chunks for WiFi networks")

        except Exception as e:
            logger.error(f"Error sending WiFi networks: {e}")
            # Send error notification
            error_packet = bytes([0, 0x01]) + json.dumps({"error": str(e)}).encode('utf-8')
            self.PropertiesChanged(
                GATT_CHRC_IFACE,
                {'Value': dbus.Array([dbus.Byte(b) for b in error_packet], signature='y')},
                []
            )

        return False  # Don't repeat GLib.idle_add

    @dbus.service.method(GATT_CHRC_IFACE)
    def StartNotify(self):
        """Client wants to receive notifications."""
        logger.info("Client subscribed to WiFi networks notifications")
        self._notifying = True

    @dbus.service.method(GATT_CHRC_IFACE)
    def StopNotify(self):
        """Client no longer wants notifications."""
        logger.info("Client unsubscribed from WiFi networks notifications")
        self._notifying = False


# ============================================================================
# Post-WiFi Connection Actions
# ============================================================================

def try_announce_after_wifi():
    """
    Attempt to announce the device after WiFi connection is established.

    This runs in a background thread after successful WiFi connection.
    It waits for internet connectivity, then tries to announce.
    """
    logger.info("Scheduling post-WiFi announce attempt in 20 seconds...")

    # Wait 20 seconds for connection to fully establish
    time.sleep(20)

    # Check for internet connectivity (try a few times)
    for attempt in range(3):
        connected, msg = check_internet_connectivity()
        if connected:
            logger.info(f"Internet connectivity verified: {msg}")
            break
        logger.info(f"Waiting for internet (attempt {attempt + 1}/3)...")
        time.sleep(5)
    else:
        logger.warning("No internet connectivity after WiFi connection - skipping announce")
        return

    # Check if already announced
    if is_device_announced():
        logger.info("Device already announced - triggering jam-tailscale")
        _trigger_tailscale()
        return

    logger.info("Attempting to announce device...")

    # Gather credentials
    device_uuid = get_device_uuid()
    api_signing_public_key = get_api_signing_public_key()
    ssh_public_key = get_ssh_public_key()
    jp_image_id = get_jp_image_id()

    if not all([device_uuid, api_signing_public_key, ssh_public_key, jp_image_id]):
        logger.warning("Missing credentials for announce - skipping")
        return

    payload = {
        'deviceUuid': device_uuid,
        'apiSigningPublicKey': api_signing_public_key,
        'sshPublicKey': ssh_public_key,
        'jpImageId': jp_image_id,
    }

    logger.info(f"Announcing to {get_api_base_url()}/jam-players/announce")

    response = api_request(
        method='POST',
        path='/jam-players/announce',
        body=payload,
        signed=False
    )

    if response is None:
        logger.warning("No response from announce API")
        return

    if response.status_code in (200, 409):
        logger.info("Announce successful (or already announced)!")
        set_device_announced()
        # Now trigger Tailscale setup
        _trigger_tailscale()
    else:
        logger.warning(f"Announce failed: {response.status_code}")


def _trigger_tailscale():
    """
    Restart jam-tailscale.service to attempt Tailscale setup.
    """
    import subprocess
    try:
        logger.info("Triggering jam-tailscale.service restart...")
        subprocess.run(
            ['systemctl', 'restart', 'jam-tailscale.service'],
            timeout=10,
            capture_output=True
        )
        logger.info("jam-tailscale.service restart triggered")
    except Exception as e:
        logger.warning(f"Failed to restart jam-tailscale: {e}")


# ============================================================================
# WiFi Credentials Characteristic (WRITE)
# ============================================================================

class WiFiCredentialsCharacteristic(Characteristic):
    """
    Characteristic to write WiFi credentials.

    When the mobile app writes to this characteristic, we:
    1. Parse the JSON credentials (ssid + password)
    2. Attempt to connect to the network
    3. Update the connection status characteristic with the result

    Expected input format:
    {"ssid": "MyNetwork", "password": "secret123"}

    The connection happens asynchronously so we don't block the BLE write.
    The mobile app should subscribe to the status characteristic for updates.
    """

    def __init__(self, bus, index: int, service, status_characteristic):
        # Support both 'write' (with response) and 'write-without-response' for flexibility.
        # The mobile app uses write-without-response to avoid BLE timeouts during WiFi connection.
        super().__init__(bus, index, WIFI_CREDENTIALS_UUID, ['write', 'write-without-response'], service)
        # Reference to status characteristic so we can update it
        self.status_characteristic = status_characteristic

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='aya{sv}')
    def WriteValue(self, value, options):
        """Receive WiFi credentials and attempt to connect."""
        try:
            # Convert bytes back to string
            data = bytes(value).decode('utf-8')
            logger.info("Received WiFi credentials request")

            # Parse JSON
            credentials = json.loads(data)
            ssid = credentials.get('ssid', '')
            password = credentials.get('password', '')

            if not ssid:
                logger.warning("No SSID provided in credentials")
                self.status_characteristic.set_status('error', 'No SSID provided')
                return

            logger.info(f"Attempting to connect to WiFi: {ssid}")
            self.status_characteristic.set_status('connecting', f'Connecting to {ssid}...')

            # Run connection in a background thread so we don't block the BLE write.
            # BLE operations should complete quickly; WiFi connection can take 10-30 seconds.
            def connect_async():
                success, error_msg = connect_to_wifi(ssid, password)
                if success:
                    logger.info(f"Successfully connected to {ssid}")
                    self.status_characteristic.set_status('connected', f'Connected to {ssid}')

                    # Trigger announce + Tailscale setup in another background thread
                    # This runs 20 seconds after WiFi connects to ensure connection is stable
                    announce_thread = threading.Thread(target=try_announce_after_wifi, daemon=True)
                    announce_thread.start()
                else:
                    logger.warning(f"Failed to connect to {ssid}: {error_msg}")
                    # Map error messages to iOS-compatible status values
                    if 'password' in error_msg.lower() or 'secrets' in error_msg.lower():
                        self.status_characteristic.set_status('invalid_password', error_msg)
                    elif 'not found' in error_msg.lower() or 'no network' in error_msg.lower():
                        self.status_characteristic.set_status('network_not_found', error_msg)
                    elif 'timeout' in error_msg.lower():
                        self.status_characteristic.set_status('timeout', error_msg)
                    else:
                        self.status_characteristic.set_status('failed', error_msg)

            thread = threading.Thread(target=connect_async, daemon=True)
            thread.start()

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in WiFi credentials: {e}")
            self.status_characteristic.set_status('failed', 'Invalid data format')
        except Exception as e:
            logger.error(f"Error processing WiFi credentials: {e}")
            self.status_characteristic.set_status('failed', str(e))


# ============================================================================
# Connection Status Characteristic (READ + NOTIFY)
# ============================================================================

class ConnectionStatusCharacteristic(Characteristic):
    """
    Characteristic to read and get notifications about connection status.

    This characteristic supports:
    - READ: Mobile app can poll current status
    - NOTIFY: Mobile app subscribes to receive status updates automatically

    Status values (must match iOS ConnectionState enum):
    - 'idle': Initial state
    - 'connecting': Currently attempting to connect
    - 'connected': Successfully connected to WiFi
    - 'failed': Generic connection failure
    - 'invalid_password': Wrong password
    - 'network_not_found': SSID not found
    - 'timeout': Connection timed out

    Response format: Just the status string (e.g., "connected")
    """

    def __init__(self, bus, index: int, service):
        super().__init__(bus, index, CONNECTION_STATUS_UUID, ['read', 'notify'], service)
        self._status = 'idle'
        self._notifying = False

    def set_status(self, status: str, message: str = ''):
        """
        Update the connection status.

        Args:
            status: One of 'idle', 'connecting', 'connected', 'failed',
                   'invalid_password', 'network_not_found', 'timeout'
            message: Human-readable message (for logging only)
        """
        self._status = status
        logger.info(f"Connection status: {status} - {message}")

        # If a client is subscribed to notifications, push the update
        if self._notifying:
            self._notify_status()

    def _notify_status(self):
        """Send a notification to subscribed clients."""
        value = self._get_status_value()
        self.PropertiesChanged(GATT_CHRC_IFACE, {'Value': value}, [])

    def _get_status_value(self):
        """Return just the status string as bytes."""
        return dbus.Array([dbus.Byte(b) for b in self._status.encode('utf-8')], signature='y')

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='a{sv}', out_signature='ay')
    def ReadValue(self, options):
        """Return current connection status."""
        logger.debug("Client requested connection status")
        return self._get_status_value()

    @dbus.service.method(GATT_CHRC_IFACE)
    def StartNotify(self):
        """
        D-Bus method: Client wants to receive notifications.

        Called when the mobile app subscribes to status updates.
        After this, any status changes will be pushed to the client.
        """
        logger.info("Client subscribed to status notifications")
        self._notifying = True

    @dbus.service.method(GATT_CHRC_IFACE)
    def StopNotify(self):
        """
        D-Bus method: Client no longer wants notifications.

        Called when the mobile app unsubscribes from status updates.
        """
        logger.info("Client unsubscribed from status notifications")
        self._notifying = False


# ============================================================================
# Device Info Characteristic (READ)
# ============================================================================

class DeviceInfoCharacteristic(Characteristic):
    """
    Characteristic to read device information.

    Returns basic info about the JAM Player for the mobile app.
    Includes device UUID, public keys for provisioning, and status flags.

    Response format (camelCase to match JAM API conventions):
    {
        "deviceUuid": "019beb00-486a-702b-9e48-6b40f233fb75",
        "bleDeviceName": "JAM-PLAYER-FB75",
        "jpImageId": "JAM-2025-01-A",
        "softwareVersion": "2.0",
        "apiSigningPublicKey": "base64-encoded-key",
        "sshPublicKey": "ssh-ed25519 AAAA...",
        "isConnected": false,
        "isAnnounced": false,
        "isRegistered": false
    }
    """

    def __init__(self, bus, index: int, service):
        super().__init__(bus, index, DEVICE_INFO_UUID, ['read'], service)

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='a{sv}', out_signature='ay')
    def ReadValue(self, options):
        """Return device information."""
        logger.debug("Client requested device info")

        device_uuid = get_device_uuid() or 'unknown'
        ble_device_name = get_device_name()
        jp_image_id = get_jp_image_id() or ''
        api_signing_public_key = get_api_signing_public_key() or ''
        ssh_public_key = get_ssh_public_key() or ''

        # Check connectivity by reading flag file maintained by jam-ble-state-manager
        # This is fast (just checking if file exists) and accurate (based on actual
        # internet verification, not just NetworkManager state).
        # The flag is updated every ~30 seconds by jam-ble-state-manager.
        is_connected = INTERNET_VERIFIED_FLAG.exists()

        # Check registration status flags
        is_announced = is_device_announced()
        is_registered = is_device_registered()

        info = {
            'deviceUuid': device_uuid,
            'bleDeviceName': ble_device_name,
            'jpImageId': jp_image_id,
            'softwareVersion': '2.0',
            'apiSigningPublicKey': api_signing_public_key,
            'sshPublicKey': ssh_public_key,
            'isConnected': is_connected,
            'isAnnounced': is_announced,
            'isRegistered': is_registered,
        }
        logger.debug(f"Returning device info: uuid={device_uuid}, connected={is_connected}, announced={is_announced}, registered={is_registered}")
        data = json.dumps(info)
        return dbus.Array([dbus.Byte(b) for b in data.encode('utf-8')])


# ============================================================================
# Provisioning Confirmation Characteristic (WRITE)
# ============================================================================

class ProvisioningConfirmCharacteristic(Characteristic):
    """
    Characteristic to confirm backend provisioning completed.

    After the iOS app successfully registers the device with the JAM backend,
    it writes the confirmation to this characteristic. This creates a local
    flag file so the device knows it's fully provisioned.

    This ensures the device won't show as "needs setup" after a reboot, and
    prevents re-provisioning attempts that could create duplicate records.

    Expected input format (JSON):
    {
        "jamPlayerId": "uuid-from-backend",
        "provisionedBy": "user-id"  // optional
    }

    Response: "OK" on success, error message on failure
    """

    def __init__(self, bus, index: int, service):
        # Support both write types for flexibility
        super().__init__(bus, index, PROVISION_CONFIRM_UUID, ['write', 'write-without-response'], service)

    @dbus.service.method(GATT_CHRC_IFACE, in_signature='aya{sv}')
    def WriteValue(self, value, options):
        """Receive and store provisioning confirmation."""
        try:
            data = bytes(value).decode('utf-8')
            logger.info("Received provisioning confirmation")

            confirmation = json.loads(data)
            jam_player_id = confirmation.get('jamPlayerId')

            if not jam_player_id:
                logger.error("Provisioning confirmation missing jamPlayerId")
                return

            # Mark device as registered (creates .registered flag file)
            success = set_device_registered()

            if success:
                logger.info(f"Device registration confirmed: {jam_player_id}")
            else:
                logger.error("Failed to store provisioning confirmation")

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in provisioning confirmation: {e}")
        except Exception as e:
            logger.error(f"Error processing provisioning confirmation: {e}")


# ============================================================================
# JAM Provisioning Service (combines all characteristics)
# ============================================================================

class JAMProvisioningService(Service):
    """
    GATT Service for JAM Player WiFi provisioning.

    This is the main service that the mobile app interacts with.
    It contains all our characteristics for WiFi configuration.
    """

    def __init__(self, bus, index: int):
        super().__init__(bus, index, JAM_SERVICE_UUID, True)

        # Create the status characteristic first since credentials needs a reference to it
        status_chrc = ConnectionStatusCharacteristic(bus, 0, self)
        self.add_characteristic(status_chrc)

        # Create other characteristics
        self.add_characteristic(WiFiNetworksCharacteristic(bus, 1, self))
        self.add_characteristic(WiFiCredentialsCharacteristic(bus, 2, self, status_chrc))
        self.add_characteristic(DeviceInfoCharacteristic(bus, 3, self))
        self.add_characteristic(ProvisioningConfirmCharacteristic(bus, 4, self))


# ============================================================================
# Helper Functions
# ============================================================================

def get_device_name() -> str:
    """
    Generate the BLE device name from the device UUID.

    Format: JAM-PLAYER-XXXXX (last 5 characters of UUID, uppercased)

    This name is shown when users scan for Bluetooth devices.
    The last 5 characters help identify which JAM Player is which
    when multiple are in range.
    """
    suffix = get_device_uuid_short(5) or 'XXXXX'
    return f"JAM-PLAYER-{suffix}"


def get_status_flags() -> int:
    """
    Calculate the status flags byte for BLE advertisement.

    The flags byte encodes device state so the mobile app can show
    status in the scan list before connecting:

    - Bit 0 (value 1): isConnected - has verified internet connectivity
    - Bit 1 (value 2): isAnnounced - announced to JAM backend
    - Bit 2 (value 4): isRegistered - fully registered via mobile app

    Returns:
        Integer 0-7 representing the combined status flags
    """
    flags = 0

    # Bit 0: isConnected (internet connectivity verified)
    if INTERNET_VERIFIED_FLAG.exists():
        flags |= 0x01

    # Bit 1: isAnnounced
    if is_device_announced():
        flags |= 0x02

    # Bit 2: isRegistered
    if is_device_registered():
        flags |= 0x04

    logger.info(f"Status flags for advertisement: {flags} "
                f"(connected={bool(flags & 0x01)}, "
                f"announced={bool(flags & 0x02)}, "
                f"registered={bool(flags & 0x04)})")

    return flags


def find_adapter(bus) -> Optional[str]:
    """
    Find the first available Bluetooth adapter.

    Bluetooth adapters in BlueZ are registered at paths like /org/bluez/hci0.
    We find one that supports GATT (BLE).

    Returns:
        D-Bus object path of the adapter, or None if not found
    """
    try:
        # Query BlueZ for all managed objects
        remote_om = dbus.Interface(
            bus.get_object(BLUEZ_SERVICE_NAME, '/'),
            DBUS_OM_IFACE
        )
        objects = remote_om.GetManagedObjects()

        # Find an object that implements GattManager1 (BLE GATT support)
        for path, interfaces in objects.items():
            if GATT_MANAGER_IFACE in interfaces:
                return path

        return None
    except Exception as e:
        logger.error(f"Error finding Bluetooth adapter: {e}")
        return None


def configure_adapter(bus, adapter_path: str):
    """
    Configure the Bluetooth adapter for our use case.

    - Disable pairing requirement (no iOS pairing popup)
    - Make adapter discoverable
    - Set appropriate power state
    """
    try:
        adapter = dbus.Interface(
            bus.get_object(BLUEZ_SERVICE_NAME, adapter_path),
            DBUS_PROP_IFACE
        )

        # Adapter1 interface for setting properties
        adapter_iface = 'org.bluez.Adapter1'

        # Ensure adapter is powered on
        adapter.Set(adapter_iface, 'Powered', dbus.Boolean(True))
        logger.info("Bluetooth adapter powered on")

        # Disable pairing requirement - this prevents the iOS pairing popup
        # Pairable=False means we don't initiate or accept pairing
        adapter.Set(adapter_iface, 'Pairable', dbus.Boolean(False))
        logger.info("Pairing disabled (no popup)")

        # Make discoverable (for scanning)
        adapter.Set(adapter_iface, 'Discoverable', dbus.Boolean(True))
        logger.info("Adapter set to discoverable")

    except Exception as e:
        logger.warning(f"Could not configure adapter (non-fatal): {e}")


def register_advertisement(bus, adapter_path: str, advertisement):
    """
    Register our advertisement with BlueZ.

    Once registered, BlueZ will start broadcasting our advertisement
    so phones can discover us.
    """
    ad_manager = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE_NAME, adapter_path),
        LE_ADVERTISING_MANAGER_IFACE
    )
    ad_manager.RegisterAdvertisement(
        advertisement.get_path(),
        {},
        reply_handler=lambda: logger.info("Advertisement registered successfully"),
        error_handler=lambda e: logger.error(f"Failed to register advertisement: {e}")
    )


def register_application(bus, adapter_path: str, application):
    """
    Register our GATT application with BlueZ.

    Once registered, BlueZ will expose our services and characteristics
    to connected BLE clients.
    """
    service_manager = dbus.Interface(
        bus.get_object(BLUEZ_SERVICE_NAME, adapter_path),
        GATT_MANAGER_IFACE
    )
    service_manager.RegisterApplication(
        application.get_path(),
        {},
        reply_handler=lambda: logger.info("GATT application registered successfully"),
        error_handler=lambda e: logger.error(f"Failed to register application: {e}")
    )




# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    """
    Main entry point for the BLE provisioning service.

    Startup sequence:
    1. Initialize D-Bus connection
    2. Find Bluetooth adapter
    3. Create GATT application with our service
    4. Create advertisement
    5. Register with BlueZ
    6. Run main event loop (handles BLE events)
    """
    log_service_start(logger, 'JAM BLE Provisioning Service')

    # Initialize D-Bus with GLib main loop integration.
    # This allows D-Bus to work with GLib's event loop.
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    # Connect to the system D-Bus (BlueZ runs on system bus, not session bus)
    try:
        bus = dbus.SystemBus()
    except Exception as e:
        logger.error(f"Failed to connect to system D-Bus: {e}")
        sys.exit(1)

    # Find a Bluetooth adapter that supports BLE GATT
    adapter_path = find_adapter(bus)
    if not adapter_path:
        logger.error("No Bluetooth adapter found - is Bluetooth enabled?")
        sd_notifier.notify("STATUS=No Bluetooth adapter")
        sys.exit(1)

    logger.info(f"Using Bluetooth adapter: {adapter_path}")

    # Configure adapter (disable pairing popup, etc.)
    configure_adapter(bus, adapter_path)

    # Generate our BLE device name (JAM-PLAYER-XXXXX)
    device_name = get_device_name()
    logger.info(f"BLE device name: {device_name}")

    # Pre-populate WiFi network cache before accepting BLE connections
    # This ensures the first read doesn't return an empty list
    trigger_wifi_scan()

    # Create our GATT application with the provisioning service
    app = Application(bus)
    service = JAMProvisioningService(bus, 0)
    app.add_service(service)

    # Get current status flags for advertisement
    # This is a snapshot at service start - status in scan list reflects this moment
    status_flags = get_status_flags()

    # Create the advertisement that makes us discoverable
    advertisement = Advertisement(bus, 0, device_name, status_flags)

    # Create GLib main loop - this is the event loop that processes D-Bus events
    mainloop = GLib.MainLoop()

    # Setup graceful shutdown on SIGTERM (systemctl stop) and SIGINT (Ctrl+C)
    setup_signal_handlers(mainloop.quit, logger)

    # Register our application and advertisement with BlueZ
    try:
        register_application(bus, adapter_path, app)
        register_advertisement(bus, adapter_path, advertisement)
    except Exception as e:
        logger.error(f"Failed to register with BlueZ: {e}")
        sys.exit(1)

    # Setup systemd watchdog pinging
    setup_glib_watchdog(WATCHDOG_INTERVAL)

    # Tell systemd we're ready to serve requests
    sd_notifier.notify("READY=1")
    sd_notifier.notify(f"STATUS=Advertising as {device_name}")
    logger.info(f"BLE provisioning service ready - advertising as {device_name}")

    # Run the main event loop.
    # This blocks and processes D-Bus events (BLE read/write requests).
    # It exits when mainloop.quit() is called (on shutdown signal).
    try:
        mainloop.run()
    except Exception as e:
        logger.exception(f"Main loop error: {e}")
    finally:
        logger.info("BLE provisioning service stopped")


if __name__ == '__main__':
    main()
