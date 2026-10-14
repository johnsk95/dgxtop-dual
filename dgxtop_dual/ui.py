"""Stacked, naturally sized Rich dashboards in the original dgxtop style.

The renderable has no terminal-height constraint.  The application renders it to
lines and scrolls that result, so a small terminal never silently loses a host.
Full mode grows with the number of disks and interfaces; below 100 columns the
I/O tables stack.  Larger values also trigger stacking when both tables would
not fit side by side.
"""

from collections import deque
import math
from typing import Any, Dict, Iterable, Mapping

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


SPARK_CHARS = "▁▂▃▄▅▆▇█"
MIB = 1024**2
GIB = 1024**3


def _number(value: Any, default: float = 0.0) -> float:
    """Keep unavailable or non-finite sensor values out of bars and tables."""
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _line(value: str, style: str = "green") -> Text:
    return Text(value, style=style, no_wrap=True, overflow="ellipsis")


class DualUI:
    """Render host state dictionaries without taking ownership of sampling."""

    def __init__(self, full: bool = False):
        self.full = full
        self._histories: Dict[str, Dict[str, deque]] = {}
        self._last_sample: Dict[str, Any] = {}
        self._measurement_console = Console(width=10000, color_system=None)

    @staticmethod
    def _bar(percent: float, width: int) -> Text:
        width = max(3, width)
        filled = round(width * max(0, min(100, percent)) / 100)
        result = Text()
        result.append("█" * filled, style="green")
        result.append("░" * (width - filled), style="bright_black")
        return result

    @staticmethod
    def _sparkline(values: Iterable[float], width: int, ceiling: float = 100) -> str:
        width = max(0, width)
        values = list(values)
        if not values or not width:
            return ""
        # Stretch the retained time window to the panel width, including when
        # only the first few samples have arrived.  Keep the latest at the right.
        if width == 1:
            values = values[-1:]
        else:
            scale = (len(values) - 1) / (width - 1)
            values = [values[round(index * scale)] for index in range(width)]
        ceiling = ceiling or max(values, default=1) or 1
        return "".join(
            SPARK_CHARS[max(1 if value > 0 else 0, min(7, round(value / ceiling * 7)))]
            for value in values
        )

    def _history_line(self, values: Iterable[float], width: int) -> Text:
        line = _line("History: ", "dim")
        line.append(self._sparkline(values, width - 9), style="green")
        return line

    @staticmethod
    def _panel(content: Any, title: str, height: int = None) -> Panel:
        return Panel(
            content,
            title=_line(title, "bold green"),
            border_style="green",
            padding=(0, 1),
            height=height,
        )

    @staticmethod
    def _pair(left: Any, right: Any) -> Table:
        row = Table.grid(expand=True, padding=0)
        row.add_column(ratio=1)
        row.add_column(ratio=1)
        row.add_row(left, right)
        return row

    def _record(self, key: str, stats: Mapping[str, Any]) -> Dict[str, deque]:
        history = self._histories.setdefault(
            key, {name: deque(maxlen=60) for name in ("cpu", "gpu", "memory")}
        )
        if not stats:
            return history
        token = (stats.get("sequence"), stats.get("timestamp"))
        if token == (None, None):
            token = id(stats)
        if self._last_sample.get(key) == token:
            return history
        self._last_sample[key] = token
        for name, field in (
            ("cpu", "usage_percent"),
            ("gpu", "utilization_gpu"),
            ("memory", "usage_percent"),
        ):
            sample = stats.get(name)
            if sample:
                history[name].append(_number(sample.get(field)))
        return history

    def _usage_line(self, percent: float, width: int) -> Text:
        prefix = f"Usage: {percent:5.1f}% ["
        line = _line(prefix)
        line.append(self._bar(percent, width - len(prefix) - 1))
        line.append("]")
        return line

    def _cpu_panel(self, stats: Mapping, history: Mapping, width: int) -> Panel:
        cpu = stats.get("cpu") or {}
        if not cpu:
            return self._panel(_line("Waiting for CPU data", "dim"), "CPU", 7)
        frequency = _number(cpu.get("frequency_mhz"))
        maximum = _number(cpu.get("frequency_max_mhz"))
        frequency_text = (
            f"Freq: {frequency:5.0f} / {maximum:.0f} MHz"
            if maximum > 0 else f"Freq: {frequency:5.0f} MHz"
        )
        content = Group(
            self._usage_line(_number(cpu.get("usage_percent")), width),
            _line(f"Temp: {_number(cpu.get('temperature_celsius')):5.1f}°C"),
            _line(frequency_text),
            Text(""),
            self._history_line(history["cpu"], width),
        )
        return self._panel(content, f"CPU ({int(_number(cpu.get('core_count')))}-core ARM)", 7)

    def _gpu_panel(self, stats: Mapping, history: Mapping, width: int) -> Panel:
        gpu = stats.get("gpu") or {}
        if not gpu:
            message = "GPU unavailable (nvidia-smi)" if stats else "Waiting for GPU data"
            return self._panel(_line(message, "dim"), "GPU (Blackwell)", 7)
        frequency = _number(gpu.get("clock_graphics"))
        maximum = _number(gpu.get("clock_max"))
        frequency_text = (
            f"Freq: {frequency:5.0f} / {maximum:.0f} MHz"
            if maximum > 0 else f"Freq: {frequency:5.0f} MHz"
        )
        power = f"{_number(gpu.get('power_draw')):.1f}W"
        power_limit = _number(gpu.get("power_limit"))
        if power_limit > 0:
            power = f"{_number(gpu.get('power_draw')):.1f}/{power_limit:.0f}W"
        content = Group(
            self._usage_line(_number(gpu.get("utilization_gpu")), width),
            _line(f"Temp: {_number(gpu.get('temperature')):.1f}°C"),
            _line(frequency_text),
            _line(f"Power: {power}"),
            self._history_line(history["gpu"], width),
        )
        return self._panel(content, f"GPU ({gpu.get('name') or 'Blackwell'})", 7)

    def _memory_panel(self, stats: Mapping, history: Mapping, width: int) -> Panel:
        memory = stats.get("memory") or {}
        if not memory:
            return self._panel(_line("Waiting for memory data", "dim"), "Unified Memory", 4)
        percent = _number(memory.get("usage_percent"))
        prefix = (
            f"RAM: {_number(memory.get('used')) / GIB:5.1f} / "
            f"{_number(memory.get('total')) / GIB:.0f} GiB ["
        )
        suffix = f"] {percent:.1f}%"
        usage = _line(prefix)
        usage.append(self._bar(percent, width - len(prefix) - len(suffix)))
        usage.append(suffix)
        return self._panel(
            Group(usage, self._history_line(history["memory"], width)),
            "Unified Memory", 4,
        )

    def _disk_history_panel(self, stats: Mapping, width: int, height: int = 4) -> Panel:
        history = stats.get("disk_history") or {}
        lines = []
        for field, label in (("read", "Read:  "), ("write", "Write: ")):
            values = [max(0, _number(value)) / MIB for value in history.get(field, [])]
            if not values:
                lines.append(_line(label + "Waiting for data", "dim"))
                continue
            suffix = f" {values[-1]:.2f} MiB/s"
            line = _line(label, "dim")
            line.append(
                self._sparkline(values, width - len(label) - len(suffix), max(values)),
                style="green",
            )
            line.append(suffix, style="green")
            lines.append(line)
        return self._panel(Group(*lines), "Disk History", height)

    @staticmethod
    def _count(value: Any) -> str:
        number = max(0, _number(value))
        if number >= 1_000_000:
            return f"{number / 1_000_000:.1f}M"
        if number >= 10_000:
            return f"{number / 1000:.1f}k"
        return f"{number:.0f}"

    @staticmethod
    def _io_table(headers: Iterable[str]) -> Table:
        table = Table(
            box=None, show_header=True, header_style="bold green",
            padding=(0, 1), pad_edge=False, collapse_padding=True, expand=True,
        )
        for index, label in enumerate(headers):
            table.add_column(
                label, justify="left" if index == 0 else "right",
                style="bold" if index == 0 else "green",
                no_wrap=True, overflow="ellipsis",
                min_width=len(label),
            )
        return table

    def _disk_table(self, stats: Mapping) -> Panel:
        table = self._io_table(("Device", "Read", "Write", "R/s", "W/s", "Await"))
        devices = stats.get("disk") or {}
        for device, sample in devices.items():
            table.add_row(
                str(device), f"{_number(sample.get('read_rate')) / MIB:.2f}",
                f"{_number(sample.get('write_rate')) / MIB:.2f}",
                self._count(sample.get("r_iops")), self._count(sample.get("w_iops")),
                f"{_number(sample.get('await_ms')):.2f}",
            )
        if not devices:
            table.add_row("No devices", "—", "—", "—", "—", "—")
        return self._panel(table, "Disk I/O (MiB/s, IOPS, ms)")

    def _network_table(self, stats: Mapping) -> Panel:
        table = self._io_table(("Interface", "RX", "TX", "RXp/s", "TXp/s", "Err"))
        interfaces = stats.get("network_io") or {}
        for interface, sample in interfaces.items():
            errors = _number(sample.get("rx_errors")) + _number(sample.get("tx_errors"))
            table.add_row(
                str(interface), f"{_number(sample.get('rx_rate')) / MIB:.2f}",
                f"{_number(sample.get('tx_rate')) / MIB:.2f}",
                self._count(sample.get("rx_packets")), self._count(sample.get("tx_packets")),
                self._count(errors),
            )
        if not interfaces:
            table.add_row("No interfaces", "—", "—", "—", "—", "—")
        return self._panel(table, "Network I/O (MiB/s, pkt/s)")

    @staticmethod
    def _status(state: Mapping, interval: float) -> tuple:
        status = str(state.get("status") or "connecting")
        stats = state.get("stats") or {}
        # The transport marks stalled streams stale; rendering need not track
        # elapsed time or change an otherwise identical frame between samples.
        live = status in ("online", "connected", "live") and bool(stats)
        if live:
            return _line("● ONLINE"), "green", True
        label = f"● {status.upper() if status != 'online' else 'STALE'}"
        if stats:
            label += " · showing last sample"
        else:
            label += " · waiting for first sample"
        return _line(label, "yellow"), "yellow", False

    def _host_panel(self, state: Mapping, width: int, interval: float) -> Panel:
        stats = state.get("stats") or {}
        key = str(state.get("address") or state.get("name") or "host")
        history = self._record(key, stats)
        content_width = max(20, width - 4)
        half_content_width = max(12, content_width // 2 - 4)
        status_line, border, live = self._status(state, interval)
        rows = [status_line]
        if state.get("error"):
            rows.append(_line(str(state["error"]), "red"))
        rows.extend((
            self._pair(
                self._cpu_panel(stats, history, half_content_width),
                self._gpu_panel(stats, history, half_content_width),
            ),
            self._memory_panel(stats, history, content_width - 4),
        ))
        if self.full:
            disk, network = self._disk_table(stats), self._network_table(stats)
            table_width = max(
                self._measurement_console.measure(table).maximum
                for table in (disk, network)
            )
            if width >= 100 and table_width <= content_width // 2:
                # Match both disk boxes and stretch Network I/O across their
                # combined height, even when many interfaces need more room.
                disk_height = max(
                    4, len(stats.get("disk") or {}) + 3,
                    math.ceil((len(stats.get("network_io") or {}) + 3) / 2),
                )
                disk.height = disk_height
                network.height = disk_height * 2
                disk_history = self._disk_history_panel(
                    stats, half_content_width, disk_height,
                )
                rows.append(self._pair(Group(disk_history, disk), network))
            else:
                disk.height = max(4, len(stats.get("disk") or {}) + 3)
                rows.extend((
                    self._disk_history_panel(stats, content_width - 4, disk.height),
                    disk, network,
                ))
        label = "SPARK 1" if state.get("local") else "SPARK 2"
        hostname = str(state.get("name") or "")
        title = f"{label} ({hostname})" if hostname and hostname != label else label
        return Panel(
            Group(*rows), title=_line(title, f"bold {border}"),
            border_style=border, padding=(0, 1),
            style="" if live or not stats else "dim",
        )

    def render(self, states: Iterable[Mapping], width: int, height: int, interval: float):
        """Return all host panels; ``height`` is intentionally left to scrolling."""
        return Group(*(self._host_panel(state, width, interval) for state in states))
