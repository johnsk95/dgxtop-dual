#!/usr/bin/env python3
"""NVIDIA GPU monitoring via nvidia-smi for DGX SPARK"""

import subprocess
import csv
import math
import shutil
from dataclasses import dataclass
from typing import Optional


@dataclass
class GPUStats:
    """Container for NVIDIA GPU statistics"""

    index: int
    name: str
    utilization_gpu: float  # 0-100%
    temperature: float  # Celsius
    power_draw: float  # Watts
    power_limit: float  # Watts
    fan_speed: Optional[float]  # % (may be N/A)
    clock_graphics: float = 0.0  # MHz - current graphics clock
    clock_max: float = 0.0  # MHz - max graphics clock


class GPUMonitor:
    """Monitor NVIDIA GPU statistics via nvidia-smi"""

    QUERY_FIELDS = (
        "index,name,utilization.gpu,"
        "temperature.gpu,power.draw,power.limit,fan.speed,"
        "clocks.current.graphics,clocks.max.graphics"
    )

    def __init__(self):
        self.last_stats: Optional[GPUStats] = None
        self._available = self._check_nvidia_smi()

    def _check_nvidia_smi(self) -> bool:
        """Check if nvidia-smi is available"""
        return shutil.which("nvidia-smi") is not None

    @property
    def is_available(self) -> bool:
        return self._available

    def get_stats(self) -> Optional[GPUStats]:
        """Query GPU statistics via nvidia-smi"""
        # A failed query must not make old GPU readings appear current.
        self.last_stats = None
        if not self._available:
            return None

        try:
            cmd = [
                "nvidia-smi",
                f"--query-gpu={self.QUERY_FIELDS}",
                "--format=csv,noheader,nounits",
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)

            if result.returncode != 0:
                return None

            rows = list(csv.reader(result.stdout.strip().splitlines()))
            if not rows or len(rows[0]) < 9:
                return None
            # Each Spark has one GPU; retain the first device on other systems.
            values = [value.strip() for value in rows[0]]

            def safe_float(v, default=0.0):
                try:
                    value = float(v)
                    return value if math.isfinite(value) else default
                except (ValueError, TypeError):
                    return default

            def safe_int(v, default=0):
                try:
                    return int(float(v)) if v not in ["[N/A]", "N/A", ""] else default
                except (ValueError, TypeError):
                    return default

            stats = GPUStats(
                index=safe_int(values[0]),
                name=values[1].strip(),
                utilization_gpu=safe_float(values[2]),
                temperature=safe_float(values[3]),
                power_draw=safe_float(values[4]),
                power_limit=safe_float(values[5]),
                fan_speed=safe_float(values[6], default=None),
                clock_graphics=safe_float(values[7]) if len(values) > 7 else 0.0,
                clock_max=safe_float(values[8]) if len(values) > 8 else 0.0,
            )

            self.last_stats = stats
            return stats

        except (OSError, subprocess.SubprocessError, csv.Error, ValueError):
            return None
