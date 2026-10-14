"""Dependency-free metrics collection shared by local and SSH workers."""

from dataclasses import asdict
import json
import math
import os
import socket
import sys
import time

from .monitors.disk_monitor import DiskMonitor
from .monitors.gpu_monitor import GPUMonitor
from .monitors.network_monitor import NetworkMonitor
from .monitors.system_monitor import SystemMonitor


class Collector:
    """Collect one host's metrics; retain counter baselines and 60 sample histories."""

    def __init__(self, full: bool = False):
        self.full = full
        self.hostname = socket.gethostname()
        self.system = SystemMonitor()
        self.gpu = GPUMonitor()
        self.disk = DiskMonitor() if full else None
        self.network = NetworkMonitor() if full else None
        self.sequence = 0

    def sample(self) -> dict:
        """Return a JSON-safe snapshot. First-sample counter rates are zero."""
        # NetworkMonitor collects the per-interface counters in full mode.
        # The aggregate network values from SystemMonitor are never displayed.
        system = self.system.get_stats(include_network=False)
        gpu = self.gpu.get_stats()
        self.sequence += 1
        sample = {
            "hostname": self.hostname,
            "timestamp": time.time(),
            "sequence": self.sequence,
            "cpu": asdict(system["cpu"]),
            "memory": asdict(system["memory"]),
            "gpu": asdict(gpu) if gpu is not None else None,
        }
        if self.full:
            sample["disk"] = self.disk.get_device_stats_for_display()
            sample["disk_history"] = self.disk.get_history()
            sample["network_io"] = self.network.get_interface_stats_for_display()
            sample["network_history"] = self.network.get_history()
        return sample


def wire_sample(sample: dict) -> dict:
    """Keep SSH frames small; the receiving worker retains chart history.

    Collector.sample keeps its complete in-process schema. The wire schema uses
    the same UI field names, but excludes undisplayed counters and carries only
    the newest disk history point so previous points are not sent every second.
    """
    result = {
        name: sample[name] for name in ("hostname", "timestamp", "sequence")
    }
    for name, fields in (
        ("cpu", ("usage_percent", "frequency_mhz", "frequency_max_mhz",
                 "temperature_celsius", "core_count")),
        ("memory", ("total", "used", "usage_percent")),
        ("gpu", ("name", "utilization_gpu", "temperature", "power_draw",
                 "power_limit", "clock_graphics", "clock_max")),
    ):
        values = sample.get(name)
        result[name] = (
            {field: values[field] for field in fields if field in values}
            if values is not None else None
        )
    if "disk" in sample:
        result["disk"] = {
            device: {field: value for field, value in values.items()
                     if field not in ("read_util", "write_util")}
            for device, values in sample["disk"].items()
        }
        result["disk_history"] = {
            field: values[-1:] for field, values in sample["disk_history"].items()
        }
        result["network_io"] = sample["network_io"]

    def rounded(value):
        if isinstance(value, float):
            return round(value, 4)
        if isinstance(value, dict):
            return {key: rounded(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rounded(item) for item in value]
        return value

    return rounded(result)


def serve(interval: float = 1.0, full: bool = False) -> None:
    """Write newline-delimited JSON to stdout until the SSH pipe closes."""
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("interval must be a finite positive number")
    collector = Collector(full=full)
    deadline = time.monotonic()
    try:
        while True:
            print(json.dumps(wire_sample(collector.sample()), allow_nan=False,
                             separators=(",", ":")), flush=True)
            deadline += interval
            now = time.monotonic()
            # If a driver query is slow, resume without a burst of old deadlines.
            if deadline <= now:
                deadline = now + interval
            time.sleep(max(0.0, deadline - now))
    except BrokenPipeError:
        # Prevent interpreter shutdown from trying to flush the closed SSH pipe.
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
            os.close(devnull)
        except (OSError, ValueError, AttributeError):
            pass
