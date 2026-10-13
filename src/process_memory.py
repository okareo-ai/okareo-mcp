"""This process's current and peak memory, for the tool-call log line.

Reported on every ``[tool] DONE`` line so a memory jump can be tied to the
calls that caused it. Linux (Cloud Run) reports both from ``/proc``; other
systems report the peak only, from ``getrusage``.
"""

import sys


def memory_mib() -> tuple[int | None, int | None]:
    """Return ``(current, peak)`` resident memory in MiB; either may be None."""
    try:
        with open("/proc/self/status") as status:
            fields = dict(
                line.split(":", 1) for line in status if line.startswith(("VmRSS", "VmHWM"))
            )
        return (
            int(fields["VmRSS"].split()[0]) // 1024,
            int(fields["VmHWM"].split()[0]) // 1024,
        )
    except (OSError, KeyError, ValueError):
        pass
    try:
        import resource  # not on Windows, where local installs also run

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports KiB, macOS bytes.
        divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
        return None, int(peak) // divisor
    except (ImportError, OSError, ValueError):
        return None, None
