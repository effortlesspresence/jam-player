# JAM Player 2.0 Services
#
# This package contains the systemd service implementations for JP 2.0.
# All services share a common venv at /opt/jam/venv and store credentials
# securely in /etc/jam/credentials (root-only access).
#
# Services:
#   - jam_first_boot.py: Generates device UUID and key pairs on first boot
#   - jam_boot_check.py: Validates system state on every boot (TODO)
#   - jam_ble_provisioning.py: BLE GATT server for WiFi setup (TODO)
#   - jam_ble_state_manager.py: Manages BLE service based on network state (TODO)
#   - jam_health_monitor.py: Monitors all JAM services (TODO)
