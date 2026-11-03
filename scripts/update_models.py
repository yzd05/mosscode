"""Refresh the packaged model catalog before a release.

python scripts/update_models.py                 # download LiteLLM's current list
python scripts/update_models.py litellm.json    # or use a file downloaded earlier
"""

import json
import sys
from pathlib import Path

from mosscode.catalog import FILE_NAME, decode, encode, slim
from mosscode.models_command import download

TARGET = Path(__file__).resolve().parents[1] / "src" / "mosscode" / "data" / FILE_NAME


def main() -> None:
    raw = json.loads(Path(sys.argv[1]).read_bytes() if len(sys.argv) > 1 else download())
    blob = encode(slim(raw))
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_bytes(blob)
    print(f"{TARGET}: {len(decode(blob).models)} models, {len(blob) // 1024} KB")


if __name__ == "__main__":
    main()
