"""What a Windows Terminal pane runs — the member as a child, then exit 0.

Windows Terminal keeps a pane open when its process exits non-zero, and taskkill
always leaves exit code 1. Exiting 0 here closes the pane however the member ended.
"""

import subprocess
import sys


def main() -> None:
    try:
        subprocess.run([sys.executable, "-m", "mosscode", *sys.argv[1:]])
    except BaseException:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
