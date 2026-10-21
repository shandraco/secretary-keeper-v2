import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "policy_api"))


@pytest.fixture
def samples() -> Path:
    return ROOT / "samples"
