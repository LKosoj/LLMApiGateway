"""Repack the official GPT Researcher wheel for the gateway's web-only use."""

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen


UPSTREAM_VERSION = "0.17.0"
LOCAL_VERSION = "0.17.0+llmgateway.1"
UPSTREAM_NAME = f"gpt_researcher-{UPSTREAM_VERSION}-py3-none-any.whl"
UPSTREAM_URL = (
    "https://files.pythonhosted.org/packages/f4/14/"
    "c04d3bff4057d8dbddd3bc28f930c0cc41800a05bdeb4f174dff187d1389/"
    f"{UPSTREAM_NAME}"
)
UPSTREAM_SHA256 = "01a230e487b24c36810fedf6f02555fb410cc6f46a1288c9940a42333ca2be7c"
DESTINATION = Path(__file__).resolve().parents[1] / "vendor"


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        upstream = root / UPSTREAM_NAME
        with urlopen(UPSTREAM_URL, timeout=30) as response:
            package = response.read()
        if hashlib.sha256(package).hexdigest() != UPSTREAM_SHA256:
            raise ValueError("Unexpected GPT Researcher upstream wheel checksum")
        upstream.write_bytes(package)
        subprocess.run([sys.executable, "-m", "wheel", "unpack", str(upstream)], cwd=root, check=True)

        unpacked = root / f"gpt_researcher-{UPSTREAM_VERSION}"
        dist_info = unpacked / f"gpt_researcher-{UPSTREAM_VERSION}.dist-info"
        metadata = dist_info / "METADATA"
        text = metadata.read_text(encoding="utf-8")
        requirement = "Requires-Dist: unstructured (>=0.17.2)\n"
        if text.count(requirement) != 1 or text.count(f"Version: {UPSTREAM_VERSION}\n") != 1:
            raise ValueError("Unexpected GPT Researcher upstream wheel metadata")
        metadata.write_text(
            text.replace(f"Version: {UPSTREAM_VERSION}\n", f"Version: {LOCAL_VERSION}\n", 1).replace(
                requirement, 'Provides-Extra: documents\nRequires-Dist: unstructured (>=0.17.2) ; extra == "documents"\n'
            ),
            encoding="utf-8",
        )
        dist_info.rename(unpacked / f"gpt_researcher-{LOCAL_VERSION}.dist-info")
        DESTINATION.mkdir(exist_ok=True)
        subprocess.run(
            [sys.executable, "-m", "wheel", "pack", str(unpacked), "--dest-dir", str(DESTINATION)],
            check=True,
            env={**os.environ, "SOURCE_DATE_EPOCH": "1700000000"},
        )


if __name__ == "__main__":
    main()
