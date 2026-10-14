"""
Network I/O monitoring module for DGXTOP Ubuntu
Handles reading /proc/net/dev and calculating transfer speeds
"""

import time
from typing import Dict, List
from dataclasses import dataclass
from collections import deque


@dataclass
class NetworkStats:
    """Container for network interface statistics"""

    interface_name: str
    rx_bytes: int = 0  # Total bytes received
    tx_bytes: int = 0  # Total bytes transmitted
    rx_packets: int = 0  # Total packets received
    tx_packets: int = 0  # Total packets transmitted
    rx_errors: int = 0  # Receive errors
    tx_errors: int = 0  # Transmit errors
    rx_dropped: int = 0  # Receive dropped
    tx_dropped: int = 0  # Transmit dropped
    # Calculated rates (set after delta calculation)
    rx_bytes_per_sec: float = 0.0  # Receive rate
    tx_bytes_per_sec: float = 0.0  # Transmit rate
    rx_packets_per_sec: float = 0.0
    tx_packets_per_sec: float = 0.0


class NetworkMonitor:
    """Monitor all interface counters with one /proc/net/dev read per sample."""

    # Interfaces to exclude from display
    EXCLUDED_INTERFACES = ("lo", "virbr", "docker", "br-", "veth")

    def __init__(self):
        self.netdev_path = "/proc/net/dev"
        self.previous_stats: Dict[str, NetworkStats] = {}
        self.last_update_time = time.monotonic()
        # History tracking for sparklines (matches disk_monitor pattern)
        self.rx_history = deque(maxlen=60)
        self.tx_history = deque(maxlen=60)

    def _is_displayable_interface(self, interface_name: str) -> bool:
        """Check if interface should be displayed (exclude virtual interfaces)"""
        # Exclude loopback and virtual interfaces
        for prefix in self.EXCLUDED_INTERFACES:
            if interface_name.startswith(prefix):
                return False
        return True

    def _parse_net_dev(self) -> List[NetworkStats]:
        """Read each interface's counters from the same kernel snapshot."""
        stats = []
        try:
            with open(self.netdev_path, "r") as f:
                for line in f:
                    if ":" not in line:
                        continue
                    interface, counters = line.split(":", 1)
                    interface = interface.strip()
                    if not self._is_displayable_interface(interface):
                        continue
                    parts = counters.split()
                    if len(parts) < 16:
                        continue
                    try:
                        values = [int(value) for value in parts]
                    except ValueError:
                        continue
                    stats.append(NetworkStats(
                        interface_name=interface,
                        rx_bytes=values[0], rx_packets=values[1],
                        rx_errors=values[2], rx_dropped=values[3],
                        tx_bytes=values[8], tx_packets=values[9],
                        tx_errors=values[10], tx_dropped=values[11],
                    ))
        except IOError:
            pass
        return stats

    def _calculate_transfer_rates(
        self, current_stats: List[NetworkStats]
    ) -> Dict[str, NetworkStats]:
        """Calculate transfer rates based on previous stats"""
        current_time = time.monotonic()
        time_delta = current_time - self.last_update_time

        if time_delta <= 0:
            return {}

        result = {}

        for current in current_stats:
            if current.interface_name in self.previous_stats:
                previous = self.previous_stats[current.interface_name]

                # Calculate rates
                rx_bytes_delta = max(0, current.rx_bytes - previous.rx_bytes)
                tx_bytes_delta = max(0, current.tx_bytes - previous.tx_bytes)
                rx_packets_delta = max(0, current.rx_packets - previous.rx_packets)
                tx_packets_delta = max(0, current.tx_packets - previous.tx_packets)

                current.rx_bytes_per_sec = rx_bytes_delta / time_delta
                current.tx_bytes_per_sec = tx_bytes_delta / time_delta
                current.rx_packets_per_sec = rx_packets_delta / time_delta
                current.tx_packets_per_sec = tx_packets_delta / time_delta

                result[current.interface_name] = current
            else:
                # First time seeing this interface
                result[current.interface_name] = current

        # Update previous stats
        self.previous_stats = {stat.interface_name: stat for stat in current_stats}
        self.last_update_time = current_time

        # Update history for sparklines (store in bytes/sec for UI flexibility)
        # Only sum displayable interfaces to avoid double-counting
        total_rx = sum(
            s.rx_bytes_per_sec
            for s in current_stats
            if self._is_displayable_interface(s.interface_name)
        )
        total_tx = sum(
            s.tx_bytes_per_sec
            for s in current_stats
            if self._is_displayable_interface(s.interface_name)
        )
        self.rx_history.append(total_rx)
        self.tx_history.append(total_tx)

        return result

    def get_stats(self) -> Dict[str, NetworkStats]:
        """Get current network statistics with transfer rates"""
        current_stats = self._parse_net_dev()
        return self._calculate_transfer_rates(current_stats)

    def get_interface_stats_for_display(self) -> Dict[str, Dict[str, float]]:
        """Get network stats formatted for display (matches disk pattern)"""
        stats = self.get_stats()
        if not stats:
            return {}

        display_stats = {}
        for interface_name, stat in stats.items():
            if not self._is_displayable_interface(interface_name):
                continue

            display_stats[interface_name] = {
                "rx_rate": stat.rx_bytes_per_sec,
                "tx_rate": stat.tx_bytes_per_sec,
                "rx_packets": stat.rx_packets_per_sec,
                "tx_packets": stat.tx_packets_per_sec,
                "rx_errors": stat.rx_errors,
                "tx_errors": stat.tx_errors,
            }

        # Sort interfaces: Wi-Fi first, then other physical interfaces
        def get_interface_priority(interface_name: str) -> int:
            """Return priority for sorting - lower numbers appear first"""
            if interface_name.startswith("wl"):  # Wi-Fi interfaces
                return 0
            elif any(
                interface_name.startswith(prefix) for prefix in ("en", "eth", "em")
            ):  # Ethernet interfaces
                return 1
            else:
                return 2  # Other physical interfaces

        # Sort by priority, then alphabetically
        sorted_interfaces = sorted(
            display_stats.keys(), key=lambda x: (get_interface_priority(x), x)
        )

        # Create new dict with sorted order
        sorted_display_stats = {
            iface: display_stats[iface] for iface in sorted_interfaces
        }

        return sorted_display_stats

    def get_history(self) -> dict:
        """Get historical data for sparkline charts"""
        return {"rx": list(self.rx_history), "tx": list(self.tx_history)}
