"""Independent local and SSH collector streams for the two-host dashboard.

The remote collector is sent over SSH's standard input, runs from a temporary
directory, and needs only Python's standard library on the other Spark.
"""

from __future__ import annotations

import base64
from collections import deque
import io
import json
import math
import os
from pathlib import Path
import selectors
import shlex
import signal
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from typing import Any


# stdin stays open after the archive arrives. Its EOF also stops a remote
# collector when the local SSH process exits, including while no sample is due.
_BOOTSTRAP = r"""
import base64, io, os, signal, sys, tempfile, threading, zipfile
payload = sys.stdin.buffer.readline()
with tempfile.TemporaryDirectory(prefix="dgxtop-dual-") as directory:
    zipfile.ZipFile(io.BytesIO(base64.b64decode(payload))).extractall(directory)
    sys.path.insert(0, directory)
    def exit_on_closed_input():
        sys.stdin.buffer.read()
        os.kill(os.getpid(), signal.SIGTERM)
    def stop(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    threading.Thread(target=exit_on_closed_input, daemon=True).start()
    from dgxtop_dual.collector import serve
    try:
        serve(interval=float(sys.argv[1]), full=sys.argv[2] == "1")
    except (BrokenPipeError, KeyboardInterrupt):
        pass
"""


@lru_cache(maxsize=1)
def _collector_payload() -> bytes:
    """Bundle only the stdlib collector, without importing the UI package."""
    package = Path(__file__).resolve().parent
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("dgxtop_dual/__init__.py", "")
        bundle.write(package / "collector.py", "dgxtop_dual/collector.py")
        for path in sorted((package / "monitors").rglob("*.py")):
            bundle.write(path, "dgxtop_dual/" + path.relative_to(package).as_posix())
    return base64.b64encode(archive.getvalue()) + b"\n"


@dataclass(frozen=True)
class HostSpec:
    name: str
    address: str
    local: bool = False


class HostWorker:
    """Keep one host current without blocking the other host or Rich's UI.

    Snapshots retain the last successful stats during a disconnection.
    ``last_seen`` is a local monotonic timestamp, suitable for sample age.
    Each collector has its own process so shutdown also stops slow GPU queries.
    """

    def __init__(
        self,
        spec: HostSpec,
        interval: float = 1.0,
        full: bool = False,
        ssh_user: str | None = None,
    ) -> None:
        self.spec = spec
        self.full = full
        self.ssh_user = ssh_user
        self._interval = self._validate_interval(interval)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._restart = threading.Event()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._status = "connecting"
        self._error = ""
        self._stats: dict[str, Any] | None = None
        self._last_seen: float | None = None
        # Only the newest disk rates cross the wire. Keep their history once,
        # here, rather than resending sixty old points with every sample.
        self._disk_history = {field: deque(maxlen=60) for field in ("read", "write")}

    @staticmethod
    def _validate_interval(interval: float) -> float:
        value = float(interval)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Refresh interval must be a finite positive number")
        return value

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._wake.clear()
        self._restart.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"dgxtop-dual-{self.spec.name}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            process = self._process
        if process is not None:
            self._terminate(process)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3.0)
        with self._lock:
            self._status = "stopped"

    def set_interval(self, interval: float) -> None:
        """Change sampling rate; the stream reconnects with the new interval."""
        value = self._validate_interval(interval)
        with self._lock:
            if value == self._interval:
                return
            self._interval = value
        self._restart.set()
        self._wake.set()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            status, error = self._status, self._error
            last_seen = self._last_seen
            # Every collector reports its hostname in the existing stream.
            # Use it for display without another SSH command or DNS lookup.
            hostname = self._stats.get("hostname") if self._stats else None
            if status == "online" and last_seen is not None:
                age = time.monotonic() - last_seen
                if age > self._stale_after():
                    status = "stale"
                    error = f"No fresh sample for {age:.0f}s"
            return {
                "name": hostname if isinstance(hostname, str) and hostname else self.spec.name,
                "address": self.spec.address,
                "local": self.spec.local,
                "status": status,
                "error": error,
                "stats": self._stats,
                "last_seen": last_seen,
            }

    def _stale_after(self) -> float:
        return max(12.0, self._interval * 3.0 + 3.0)

    def _command(self, interval: float) -> list[str]:
        collector_args = ["-u", "-c", _BOOTSTRAP, str(interval), "1" if self.full else "0"]
        if self.spec.local:
            return [sys.executable, *collector_args]
        command = [
            "ssh", "-T",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=5",
            "-o", "ServerAliveInterval=3",
            "-o", "ServerAliveCountMax=2",
            "-o", "StrictHostKeyChecking=accept-new",
        ]
        if self.ssh_user:
            command.extend(["-l", self.ssh_user])
        command.extend(["--", self.spec.address, shlex.join(["python3", *collector_args])])
        return command

    def _run(self) -> None:
        while not self._stop.is_set():
            self._restart.clear()
            self._wake.clear()
            with self._lock:
                self._status = "connecting"
                interval = self._interval
            try:
                self._stream(interval)
            except Exception as exc:
                if not self._stop.is_set() and not self._restart.is_set():
                    with self._lock:
                        self._status = "reconnecting"
                        self._error = str(exc)[-2048:]
            if self._stop.is_set():
                break
            if not self._restart.is_set():
                self._wake.wait(2.0)

    def _stream(self, interval: float) -> None:
        payload = _collector_payload()
        process = subprocess.Popen(
            self._command(interval),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
        )
        with self._lock:
            self._process = process
        diagnostics = bytearray()
        pending = bytearray()
        last_sample = time.monotonic()
        try:
            assert process.stdin is not None
            with selectors.DefaultSelector() as selector:
                assert process.stdout is not None and process.stderr is not None
                # A payload can exceed a pipe's capacity while SSH connects.
                # Write it as the pipe permits, keeping stop and stderr active.
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                sent = 0
                while not self._stop.is_set() and not self._restart.is_set():
                    for key, _ in selector.select(timeout=min(1.0, max(0.25, interval))):
                        if key.data == "stdin":
                            try:
                                sent += os.write(key.fd, payload[sent:])
                            except BlockingIOError:
                                continue
                            except BrokenPipeError:
                                # Drain stderr to report SSH's actual failure.
                                selector.unregister(key.fileobj)
                                continue
                            if sent == len(payload):
                                selector.unregister(key.fileobj)
                            continue
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        if key.data == "stderr":
                            diagnostics.extend(chunk)
                            del diagnostics[:-2048]
                            continue
                        pending.extend(chunk)
                        while b"\n" in pending:
                            line, _, remainder = pending.partition(b"\n")
                            pending = bytearray(remainder)
                            try:
                                sample = json.loads(line)
                            except (ValueError, UnicodeDecodeError):
                                continue  # SSH banners or other non-JSON noise.
                            if not isinstance(sample, dict) or not {
                                "hostname", "timestamp", "sequence"
                            }.issubset(sample):
                                continue
                            history = sample.get("disk_history")
                            if isinstance(history, dict):
                                for field, values in self._disk_history.items():
                                    incoming = history.get(field) or []
                                    if incoming:
                                        values.append(incoming[-1])
                                # Each published snapshot owns a separate list;
                                # UI cache invalidation relies on immutability.
                                sample["disk_history"] = {
                                    field: list(values)
                                    for field, values in self._disk_history.items()
                                }
                            last_sample = time.monotonic()
                            with self._lock:
                                self._stats = sample
                                self._last_seen = last_sample
                                self._status = "online"
                                self._error = ""
                        if len(pending) > 1024 * 1024:
                            raise RuntimeError("Collector sent an oversized sample")
                    if process.poll() is not None and not selector.get_map():
                        detail = diagnostics.decode("utf-8", errors="replace").strip()
                        raise RuntimeError(detail or f"Collector exited ({process.returncode})")
                    if time.monotonic() - last_sample > self._stale_after():
                        detail = diagnostics.decode("utf-8", errors="replace").strip()
                        raise RuntimeError(detail or "Collector stopped sending samples")
        except (BrokenPipeError, OSError) as exc:
            detail = diagnostics.decode("utf-8", errors="replace").strip()
            raise RuntimeError(detail or str(exc)) from exc
        finally:
            self._cleanup(process)
            with self._lock:
                if self._process is process:
                    self._process = None

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            try:
                # The collector and its nvidia-smi children share this group.
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    @classmethod
    def _cleanup(cls, process: subprocess.Popen[bytes]) -> None:
        cls._terminate(process)
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=1.0)
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
