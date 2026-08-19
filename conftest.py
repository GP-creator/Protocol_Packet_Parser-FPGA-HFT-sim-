"""Put the repo root on sys.path so `wirespec` and `tb.common` import without
an install step. Deliberate: the build environment forbids adding packages, and
an editable install is not needed for a source tree this small.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
