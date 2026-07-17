from __future__ import annotations

import os
import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    command = list(sys.argv[1:] if argv is None else argv)
    if not command:
        print("container-entrypoint: reason=missing-command", file=sys.stderr)
        return 64
    try:
        os.execvpe(command[0], command, os.environ)
    except OSError:
        print("container-entrypoint: reason=command-not-executable", file=sys.stderr)
        return 126


if __name__ == "__main__":
    raise SystemExit(main())
