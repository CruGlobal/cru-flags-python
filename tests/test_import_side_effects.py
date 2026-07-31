"""Import-time and shutdown-time behaviour, verified in a clean interpreter.

These have to run out-of-process: once any other test has touched the
module-level client, `import cru_flags` in *this* interpreter proves nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

# Port 9 (discard) on loopback refuses connections immediately, so the poller
# fails fast instead of waiting on a timeout.
UNREACHABLE = "http://127.0.0.1:9/flags/ararat/release-candidate"

IMPORT_ONLY = """
import threading

import cru_flags

assert cru_flags.flags._thread is None, "import must not start the poller"
strays = [t for t in threading.enumerate() if t is not threading.main_thread()]
assert not strays, strays
print("ok")
"""

START_THEN_EXIT = """
import cru_flags

cru_flags.flags.enabled("pilot_banner")  # starts the poller
assert cru_flags.flags._thread is not None
print("polling")
"""


def _run(code: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        env={**os.environ, "CRU_FLAGS_URL": UNREACHABLE},
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_import_starts_no_threads() -> None:
    result = _run(IMPORT_ONLY)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_the_poller_never_delays_interpreter_exit() -> None:
    started = time.monotonic()
    result = _run(START_THEN_EXIT, timeout=60.0)
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr
    assert "polling" in result.stdout
    # The poller sleeps ~30s between polls. A non-daemon thread would hold
    # the interpreter open for that whole interval.
    assert elapsed < 10.0, f"exit took {elapsed:.1f}s: {result.stderr}"
