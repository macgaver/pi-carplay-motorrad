"""Push a still image (and later CarPlay) to a BMW Motorrad 10.25" TFT in Map mode."""
from pathlib import Path


def _read_version() -> str:
    # One VERSION file: at the repo root in a checkout, /opt/tftpush/VERSION on the Pi (the
    # image build and the self-update write it there). Both sit next to this package folder.
    try:
        return (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip() or "0"
    except OSError:
        return "0"


__version__ = _read_version()
