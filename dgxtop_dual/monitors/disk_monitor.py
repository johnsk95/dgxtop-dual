"""
Disk I/O monitoring module for DGXTOP Ubuntu
Handles reading /proc/diskstats and calculating transfer speeds
"""

import time
import os
import shutil
import sys
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from collections import deque


@dataclass
class DiskStats:
    device_name: str
    read_bytes_per_sec: float = 0.0
    write_bytes_per_sec: float = 0.0
    read_ios_per_sec: float = 0.0
    write_ios_per_sec: float = 0.0
    utilization: float = 0.0
    sectors_read: int = 0
    sectors_written: int = 0
    read_ios: int = 0
    write_ios: int = 0
    time_elapsed: float = 0.0
    # NEW FIELDS for latency
    read_time_ms: int = 0  # Field 4 from /proc/diskstats
    write_time_ms: int = 0  # Field 8 from /proc/diskstats
    io_time_ms: int = 0  # Field 10 from /proc/diskstats
    await_read_ms: float = 0.0  # Calculated latency
    await_write_ms: float = 0.0
    await_total_ms: float = 0.0
    io_in_progress: int = 0  # Field 9 - queue depth


class DiskMonitor:
    """Monitor disk I/O statistics from /proc/diskstats"""

    def __init__(self):
        self.diskstats_path = "/proc/diskstats"
        self.previous_stats: Dict[str, DiskStats] = {}
        self.last_update_time = time.monotonic()
        # History tracking for sparklines
        self.read_history = deque(maxlen=60)
        self.write_history = deque(maxlen=60)
        # Cache mounted devices for filtering
        self._mounted_devices: set = set()
        self._update_mounted_devices()

    def _update_mounted_devices(self):
        """Update the set of mounted device names from /proc/mounts"""
        mounted = set()
        try:
            with open("/proc/mounts", "r") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        device = parts[0]
                        if device.startswith("/dev/"):
                            # Extract device name (e.g., /dev/sda1 -> sda1)
                            dev_name = device[5:]
                            mounted.add(dev_name)
        except IOError:
            pass
        self._mounted_devices = mounted

    def _is_displayable_device(self, device_name: str) -> bool:
        """Check if device should be displayed (mounted partition)"""
        # Exclude virtual devices
        if device_name.startswith(self.EXCLUDED_PREFIXES):
            return False
        # Only include known physical device prefixes
        if not device_name.startswith(self.PHYSICAL_PREFIXES):
            return False
        # Include only if it's a mounted partition
        return device_name in self._mounted_devices

    def _parse_diskstats(self) -> List[DiskStats]:
        """Parse /proc/diskstats and return disk statistics"""
        stats = []

        if not os.path.exists(self.diskstats_path):
            raise FileNotFoundError(f"{self.diskstats_path} not found")

        try:
            with open(self.diskstats_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    # Parse diskstats line
                    # Format: major minor device_name rio rmerge rsectors
                    # ruse wio wmerge wsectors wuse running use aveq
                    parts = line.split()
                    if len(parts) < 14:
                        continue

                    device_name = parts[2]
                    try:
                        # /proc/diskstats format (0-indexed after split):
                        # 0=major, 1=minor, 2=device_name
                        # 3=rio (reads completed), 4=rmerge, 5=rsectors, 6=ruse (ms reading)
                        # 7=wio (writes completed), 8=wmerge, 9=wsectors, 10=wuse (ms writing)
                        # 11=running (IOs in progress), 12=use (ms doing IOs), 13=aveq
                        read_ios = int(parts[3])           # reads completed
                        sectors_read = int(parts[5])       # sectors read
                        read_time_ms = int(parts[6])       # time spent reading (ms)
                        write_ios = int(parts[7])          # writes completed
                        sectors_written = int(parts[9])    # sectors written
                        write_time_ms = int(parts[10])     # time spent writing (ms)
                        io_in_progress = int(parts[11])    # IOs currently in progress
                        io_time_ms = int(parts[12])        # time spent doing IOs (ms)

                        stat = DiskStats(
                            device_name=device_name,
                            sectors_read=sectors_read,
                            sectors_written=sectors_written,
                            read_ios=read_ios,
                            write_ios=write_ios,
                            # NEW FIELDS
                            read_time_ms=read_time_ms,
                            write_time_ms=write_time_ms,
                            io_in_progress=io_in_progress,
                            io_time_ms=io_time_ms,
                        )
                        stats.append(stat)
                    except (ValueError, IndexError):
                        continue

        except IOError as e:
            raise IOError(f"Error reading {self.diskstats_path}: {e}")

        return stats

    def _calculate_transfer_rates(
        self, current_stats: List[DiskStats]
    ) -> Dict[str, DiskStats]:
        """Calculate transfer rates based on previous stats"""
        current_time = time.monotonic()
        time_delta = current_time - self.last_update_time

        if time_delta <= 0:
            return {}

        result = {}

        for current in current_stats:
            if current.device_name in self.previous_stats:
                previous = self.previous_stats[current.device_name]

                # Calculate rates
                read_bytes_delta = max(0, current.sectors_read - previous.sectors_read) * 512
                write_bytes_delta = (
                    max(0, current.sectors_written - previous.sectors_written)
                ) * 512
                read_ios_delta = max(0, current.read_ios - previous.read_ios)
                write_ios_delta = max(0, current.write_ios - previous.write_ios)

                current.read_bytes_per_sec = read_bytes_delta / time_delta
                current.write_bytes_per_sec = write_bytes_delta / time_delta
                current.read_ios_per_sec = read_ios_delta / time_delta
                current.write_ios_per_sec = write_ios_delta / time_delta
                current.time_elapsed = time_delta

                # Calculate await latency
                await_read, await_write, await_total = self._calculate_await(
                    current, previous
                )
                current.await_read_ms = await_read
                current.await_write_ms = await_write
                current.await_total_ms = await_total

                result[current.device_name] = current
            else:
                # First time seeing this device
                result[current.device_name] = current

        # Update previous stats
        self.previous_stats = {stat.device_name: stat for stat in current_stats}
        self.last_update_time = current_time

        # Update history for sparklines (store in bytes/sec for UI flexibility)
        # Only sum displayable (mounted) devices to avoid double-counting
        total_read = sum(
            s.read_bytes_per_sec
            for s in current_stats
            if self._is_displayable_device(s.device_name)
        )
        total_write = sum(
            s.write_bytes_per_sec
            for s in current_stats
            if self._is_displayable_device(s.device_name)
        )
        self.read_history.append(total_read)
        self.write_history.append(total_write)

        return result

    def _calculate_await(self, current: DiskStats, previous: DiskStats) -> tuple:
        """Calculate await latency in milliseconds"""
        delta_read_ios = max(0, current.read_ios - previous.read_ios)
        delta_write_ios = max(0, current.write_ios - previous.write_ios)
        delta_read_time = max(0, current.read_time_ms - previous.read_time_ms)
        delta_write_time = max(0, current.write_time_ms - previous.write_time_ms)

        await_read = delta_read_time / delta_read_ios if delta_read_ios > 0 else 0.0
        await_write = delta_write_time / delta_write_ios if delta_write_ios > 0 else 0.0

        total_ios = delta_read_ios + delta_write_ios
        total_time = delta_read_time + delta_write_time
        await_total = total_time / total_ios if total_ios > 0 else 0.0

        return await_read, await_write, await_total

    def get_disk_stats(self) -> Dict[str, DiskStats]:
        """Get current disk statistics with transfer rates"""
        try:
            self._update_mounted_devices()
            current_stats = self._parse_diskstats()
            return self._calculate_transfer_rates(current_stats)
        except Exception as e:
            print(f"Error getting disk stats: {e}", file=sys.stderr)
            return {}

    def format_bytes(self, bytes_val: float) -> str:
        """Format bytes with appropriate unit (K/M/G)"""
        if bytes_val < 1024:
            return f"{bytes_val:.1f} B/s"
        elif bytes_val < 1024**2:
            return f"{bytes_val / 1024:.1f} KB/s"
        elif bytes_val < 1024**3:
            return f"{bytes_val / 1024**2:.1f} MB/s"
        else:
            return f"{bytes_val / 1024**3:.1f} GB/s"

    def format_size(self, bytes_val: int) -> str:
        """Format bytes with appropriate unit (K/M/G) for storage size"""
        if bytes_val < 1024:
            return f"{bytes_val} B"
        elif bytes_val < 1024**2:
            return f"{bytes_val / 1024:.1f} KB"
        elif bytes_val < 1024**3:
            return f"{bytes_val / 1024**2:.1f} MB"
        elif bytes_val < 1024**4:
            return f"{bytes_val / 1024**3:.1f} GB"
        else:
            return f"{bytes_val / 1024**4:.1f} TB"

    def get_disk_summary(self) -> str:
        """Get formatted summary of disk statistics"""
        stats = self.get_disk_stats()
        if not stats:
            return "No disk statistics available"

        summary = []
        for device_name, stat in stats.items():
            summary.append(f"{device_name}:")
            summary.append(f"  Read: {self.format_bytes(stat.read_bytes_per_sec)}")
            summary.append(f"  Write: {self.format_bytes(stat.write_bytes_per_sec)}")
            summary.append(f"  R-IOPS: {stat.read_ios_per_sec:.1f}")
            summary.append(f"  W-IOPS: {stat.write_ios_per_sec:.1f}")
            summary.append("")

        return "\n".join(summary)

    def get_max_transfer_rate(self, stats: Optional[Dict[str, "DiskStats"]] = None) -> float:
        """Get maximum transfer rate for bar graph scaling.

        Args:
            stats: Optional pre-fetched stats dict. If None, will fetch fresh stats.
        """
        if stats is None:
            stats = self.get_disk_stats()
        if not stats:
            return 1024  # Default 1 KB/s

        max_rate = 0
        for stat in stats.values():
            max_rate = max(max_rate, stat.read_bytes_per_sec, stat.write_bytes_per_sec)

        return max(max_rate, 1024)  # Ensure at least 1 KB/s

    # Prefixes to exclude from display (virtual devices)
    EXCLUDED_PREFIXES = ("loop", "ram", "dm-", "sr", "fd")
    # Prefixes for physical devices we want to show
    PHYSICAL_PREFIXES = ("sd", "nvme", "vd", "hd", "xvd", "mmcblk")

    def get_device_stats_for_display(self) -> Dict[str, Dict[str, float]]:
        """Get disk stats formatted for display with bar graphs"""
        stats = self.get_disk_stats()
        if not stats:
            return {}

        display_stats = {}
        max_rate = self.get_max_transfer_rate(stats)  # Pass stats to avoid double fetch

        for device_name, stat in stats.items():
            # Filter to only show mounted partitions
            if not self._is_displayable_device(device_name):
                continue
            # Calculate utilization percentage (simplified)
            read_util = (
                (stat.read_bytes_per_sec / max_rate) * 100 if max_rate > 0 else 0
            )
            write_util = (
                (stat.write_bytes_per_sec / max_rate) * 100 if max_rate > 0 else 0
            )

            # Cap at 100% for display
            read_util = min(read_util, 100)
            write_util = min(write_util, 100)

            display_stats[device_name] = {
                "read_rate": stat.read_bytes_per_sec,
                "write_rate": stat.write_bytes_per_sec,
                "read_util": read_util,
                "write_util": write_util,
                "r_iops": stat.read_ios_per_sec,
                "w_iops": stat.write_ios_per_sec,
                "await_ms": stat.await_total_ms,
            }

        return display_stats

    def get_volume_stats(self) -> Dict[str, Dict[str, Any]]:
        """Get volume statistics including size and usage for mounted filesystems"""
        volume_stats = {}

        try:
            # Get all mounted filesystems
            with open("/proc/mounts", "r") as f:
                for line in f:
                    if line.strip():
                        parts = line.strip().split()
                        if len(parts) >= 3:
                            device, mount_point, fs_type = parts[0], parts[1], parts[2]

                            # Skip virtual filesystems and special devices
                            virtual_fs = [
                                "proc",
                                "procfs",  # Process filesystem
                                "sysfs",  # System filesystem
                                "devtmpfs",  # Device temporary filesystem
                                "tmpfs",
                                "tmpfs",  # Temporary filesystem
                                "cgroup",
                                "cgroup2",
                                "cgroupfs",  # Control groups
                                "squashfs",  # Squashed filesystem
                                "devpts",  # Device pseudo terminal filesystem
                                "securityfs",  # Security filesystem
                                "pstore",  # Persistent storage filesystem
                                "bpf",  # Berkeley Packet Filter filesystem
                                "systemd-1",
                                "cgmfs",  # Systemd filesystems
                                "mqueue",  # Message queue filesystem
                                "hugetlbfs",  # Huge page filesystem
                                "debugfs",  # Debug filesystem
                                "tracefs",  # Trace filesystem
                                "configfs",  # Configuration filesystem
                                "fusectl",  # FUSE control filesystem
                                "binfmt_misc",  # Binary format misc filesystem
                                "sunrpc",  # Sun RPC filesystem
                                "nsfs",  # Namespace filesystem
                                "nfs",
                                "nfs4",  # Network filesystems
                                "autofs",
                                "autofs4",  # Automounter filesystems
                                "fuse",
                                "fuseblk",  # FUSE filesystems
                                "iso9660",  # CD-ROM filesystem
                                "udf",  # Universal Disk Format
                                "ntfs",
                                "fat",
                                "vfat",
                                "exfat",  # Windows filesystems (if mounted via loop)
                                "zfs",  # Advanced filesystems (if system-specific)
                                "overlay",  # Overlay filesystem
                                "aufs",  # Another Union File System
                                "unionfs",  # Union filesystem
                                "ramfs",  # RAM filesystem
                            ]
                            if fs_type in virtual_fs:
                                continue

                            try:
                                # Get disk usage
                                total, used, free = shutil.disk_usage(mount_point)
                                usage_percent = (used / total) * 100 if total > 0 else 0

                                # Get device stats
                                device_stats = self.get_disk_stats()
                                read_rate = 0
                                write_rate = 0

                                # Find matching device stats using improved device name matching
                                matched_device = self._match_device_name(
                                    device, list(device_stats.keys())
                                )
                                if matched_device:
                                    stat = device_stats[matched_device]
                                    read_rate = stat.read_bytes_per_sec
                                    write_rate = stat.write_bytes_per_sec

                                volume_stats[device] = {
                                    "mount_point": mount_point,
                                    "size": self.format_size(total),
                                    "used": self.format_size(used),
                                    "free": self.format_size(free),
                                    "usage_percent": usage_percent,
                                    "read_rate": read_rate,
                                    "write_rate": write_rate,
                                }

                            except (OSError, PermissionError):
                                # Skip devices we can't access
                                continue

        except Exception as e:
            print(f"Error getting volume stats: {e}", file=sys.stderr)

        return volume_stats

    def get_history(self) -> dict:
        """Get historical data for sparkline charts"""
        return {"read": list(self.read_history), "write": list(self.write_history)}

    def _match_device_name(
        self, mount_device: str, diskstats_devices: List[str]
    ) -> Optional[str]:
        """Match mount device name to diskstats device name.

        Args:
            mount_device: Device name from /proc/mounts (e.g., /dev/sda1, /dev/nvme0n1p2)
            diskstats_devices: List of device names from /proc/diskstats (e.g., sda, nvme0n1p2)

        Returns:
            Matching device name from diskstats_devices, or None if no match found
        """
        # Extract device name from mount path (remove /dev/ prefix)
        if mount_device.startswith("/dev/"):
            base_name = mount_device[5:]  # Remove '/dev/' prefix
        else:
            base_name = mount_device

        # Try exact match first
        if base_name in diskstats_devices:
            return base_name

        # Try matching with partition numbers (e.g., sda1 -> sda)
        for dev in diskstats_devices:
            # Check if base_name starts with dev and has a number suffix
            if base_name.startswith(dev):
                suffix = base_name[len(dev) :]
                if suffix.isdigit():
                    return dev

        # Try matching with common device variations
        device_variations = [
            base_name,  # Original
            base_name.replace("nvme", "nvme"),  # NVMe devices
            base_name.replace("mmcblk", "mmcblk"),  # MMC devices
            base_name.replace("sd", "sd"),  # SCSI devices
            base_name.replace("hd", "hd"),  # Legacy devices
            "nvme" + base_name[4:]
            if base_name.startswith("nvme")
            else base_name,  # NVMe variations
            "mmcblk" + base_name[6:]
            if base_name.startswith("mmcblk")
            else base_name,  # MMC variations
        ]

        # Try each variation
        for variation in device_variations:
            if variation in diskstats_devices:
                return variation

        # Fallback: check if any diskstats device is a substring of mount device
        for dev in diskstats_devices:
            if dev in base_name or base_name in dev:
                return dev

        return None
