"""Ch15 BE5/BE7: what a Windows Terminal pane runs — the member as a child, then exit 0.

Windows Terminal keeps a pane open when its process exits non-zero (experiment E3), and taskkill
always leaves exit code 1 (E4). Exiting 0 here closes the pane however the member ended.
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
