from __future__ import annotations

import sys
from typing import TextIO


def configure_console_output(*streams: TextIO) -> None:
    """Make status messages safe on Windows systems with a legacy code page.

    The GUI already starts workers in UTF-8 mode, but direct command-line use can
    otherwise crash before inference begins when the selected UI language is
    Chinese and the active console uses a Western code page.
    """
    targets = streams or (sys.stdout, sys.stderr)
    for stream in targets:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            # Captured or embedded streams may forbid reconfiguration. They are
            # normally Unicode-capable already, so there is nothing else to do.
            continue
