from pathlib import Path

import pytest

# Real invoices are kept out of git (samples/ is ignored); tests that need one are skipped without it.
SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "8100800394 Commercial Invoice.pdf"

needs_sample = pytest.mark.skipif(not SAMPLE.is_file(), reason=f"sample invoice not found: {SAMPLE}")
