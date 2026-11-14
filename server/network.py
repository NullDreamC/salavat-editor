"""Optional process-local HTTP proxy; never changes Windows network settings."""
import json
import os
from pathlib import Path

from .db import ROOT


def http_options():
    directory = Path(os.environ.get('SALAVAT_DATA_DIR', ROOT / '.runtime'))
    try:
        proxy = json.loads((directory / 'network.json').read_text(encoding='utf-8')).get('proxy')
    except (OSError, ValueError, AttributeError):
        proxy = None
    return {'proxy': proxy} if isinstance(proxy, str) and proxy else {}
