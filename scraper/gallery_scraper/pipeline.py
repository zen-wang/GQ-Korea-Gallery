"""Per-run flow: discover -> render -> parse -> optimize -> store. Phase 3."""

from __future__ import annotations

import sys


def run() -> None:
    raise NotImplementedError("Phase 3")


if __name__ == "__main__":
    sys.exit(run())
