"""Every dashboard tile has hover text, so new metrics can't ship unexplained."""

import re
from pathlib import Path

from app.report.glossary import HINTS


def test_every_tile_has_a_hint():
    src = (Path(__file__).parent.parent / "app" / "report" / "dashboard.py").read_text()
    labels = set(re.findall(r'tile\("([^"]+)"', src))
    assert labels
    assert sorted(labels - set(HINTS)) == []
