"""Command line entry point, terminal lifecycle, and scrolling."""

import argparse
import math
import os
import select
import signal
import socket
import sys
import termios
import threading
import tty

from rich.console import Console
from rich.live import Live
from rich.segment import Segment
from rich.text import Text

from . import __version__
from .transport import HostSpec, HostWorker
from .ui import DualUI


HOSTS = (
    HostSpec(socket.gethostname(), "192.168.100.10", local=True),
    HostSpec("SPARK 2", "192.168.100.11"),
)


def _interval(value):
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("interval must be a number") from None
    if not math.isfinite(seconds) or seconds < 0.1:
        raise argparse.ArgumentTypeError("interval must be finite and at least 0.1 seconds")
    return seconds


def _parser():
    parser = argparse.ArgumentParser(
        prog="dgxtop-dual",
        description="Monitor the local Spark (192.168.100.10) and a second Spark (192.168.100.11) over SSH.",
    )
    parser.add_argument("--full", action="store_true", help="also show disk history, disk I/O, and network I/O")
    parser.add_argument("-i", "--interval", type=_interval, default=1.0, metavar="SECONDS", help="sampling interval (default: 1.0)")
    parser.add_argument("--ssh-user", metavar="USER", help="SSH login on the second Spark (default: SSH configuration or current account)")
    parser.add_argument("-v", "--version", action="version", version=f"dgxtop-dual {__version__}")
    return parser


class _Frame:
    """Render already measured lines without losing styles or terminal width."""

    def __init__(self, lines):
        self.lines = lines

    def __rich_console__(self, console, options):
        for index, line in enumerate(self.lines):
            if index:
                yield Segment.line()
            yield from line


class _Keyboard:
    """Read complete terminal key sequences, including split SSH input."""

    KEYS = {
        "\x1b[A": "up", "\x1b[B": "down",
        "\x1b[5~": "page_up", "\x1b[6~": "page_down",
        "\x1b[H": "home", "\x1b[F": "end",
        "\x1bOH": "home", "\x1bOF": "end",
        "\x1b[1~": "home", "\x1b[4~": "end",
    }

    def __init__(self):
        self.pending = ""

    def read(self):
        if select.select([sys.stdin], [], [], 0)[0]:
            self.pending += os.read(sys.stdin.fileno(), 1024).decode("ascii", errors="ignore")
        keys = []
        while self.pending:
            if self.pending[0] != "\x1b":
                keys.append(self.pending[0])
                self.pending = self.pending[1:]
                continue
            matched = next((seq for seq in self.KEYS if self.pending.startswith(seq)), None)
            if matched:
                keys.append(self.KEYS[matched])
                self.pending = self.pending[len(matched):]
            elif any(seq.startswith(self.pending) for seq in self.KEYS):
                break
            else:
                self.pending = self.pending[1:]
        return keys


def run(args, console):
    stop = threading.Event()
    workers = [HostWorker(host, interval=args.interval, full=args.full, ssh_user=args.ssh_user) for host in HOSTS]
    ui = DualUI(full=args.full)
    keyboard = _Keyboard()
    saved_terminal = None
    saved_signals = {}
    offset = 0
    page_height = 1
    total_lines = 0
    interval = args.interval
    body_key = None
    frame_key = None
    lines = []

    def stop_signal(signum, frame):
        stop.set()

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            saved_signals[sig] = signal.signal(sig, stop_signal)
        if sys.stdin.isatty():
            saved_terminal = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
        for worker in workers:
            worker.start()

        with Live(console=console, screen=True, auto_refresh=False, vertical_overflow="crop") as live:
            while not stop.is_set():
                if saved_terminal is not None:
                    for key in keyboard.read():
                        if key in ("q", "Q", "\x03"):
                            stop.set()
                        elif key in ("+", "=", "-"):
                            interval = max(0.1, round(interval + (-0.1 if key in ("+", "=") else 0.1), 2))
                            for worker in workers:
                                worker.set_interval(interval)
                        elif key in ("j", "down"):
                            offset += 1
                        elif key in ("k", "up"):
                            offset -= 1
                        elif key in ("page_down", " "):
                            offset += max(1, page_height - 2)
                        elif key == "page_up":
                            offset -= max(1, page_height - 2)
                        elif key in ("g", "home"):
                            offset = 0
                        elif key in ("G", "end"):
                            offset = total_lines
                if stop.is_set():
                    break

                width, height = console.size
                page_height = max(1, height - 2)
                states = [worker.snapshot() for worker in workers]
                options = console.options.update_width(width).reset_height()
                # A sample dictionary is replaced, never mutated, by its worker.
                # Measuring all panels is the expensive part of a Rich redraw;
                # keep those lines until data, connection state, or width changes.
                current_body_key = (width, interval, tuple(
                    (id(state["stats"]), state["status"], state["error"])
                    for state in states
                ))
                if current_body_key != body_key:
                    renderable = ui.render(states, width=width, height=page_height, interval=interval)
                    lines = console.render_lines(renderable, options, pad=True)
                    body_key = current_body_key
                total_lines = len(lines)
                offset = max(0, min(offset, total_lines - page_height))
                current_frame_key = (body_key, height, offset)
                if current_frame_key != frame_key:
                    visible = lines[offset:offset + page_height]
                    visible += [[Segment(" " * width)]] * max(0, page_height - len(visible))

                    title = Text("DUAL DGX SPARK MONITOR", style="bold green", justify="center", no_wrap=True)
                    header_lines = console.render_lines(title, options.update(height=1), pad=True)
                    footer = Text(f"dgxtop-dual | {interval:.1f}s | q quit | +/- interval", style="dim")
                    if total_lines > page_height:
                        footer.append(f" | j/k ↑↓ PgUp/PgDn g/G scroll | {offset + 1}-{min(offset + page_height, total_lines)}/{total_lines}", style="yellow")
                    footer_lines = console.render_lines(footer, options.update(height=1), pad=True)
                    live.update(_Frame(header_lines[:1] + visible + footer_lines[:1]), refresh=True)
                    frame_key = current_frame_key
                # Keyboard and terminal resizing remain responsive, but an idle
                # poll performs no Rich rendering and emits no terminal traffic.
                stop.wait(0.25)
    finally:
        # Restore the terminal before waiting for child collectors to exit.
        if saved_terminal is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved_terminal)
        for worker in workers:
            worker.stop()
        for sig, handler in saved_signals.items():
            signal.signal(sig, handler)
    return 0


def main(argv=None):
    args = _parser().parse_args(argv)
    console = Console()
    if not console.is_terminal:
        console.print("dgxtop-dual needs a terminal. Run it directly, or connect with ssh -t.", style="red")
        return 2
    try:
        return run(args, console)
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        console.print(Text(f"dgxtop-dual: {error}", style="red"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
