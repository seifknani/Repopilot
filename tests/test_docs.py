"""errors.md and threat_model.md must only reference tests that really exist."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REF = re.compile(r"(tests/test_\w+\.py)::(test_\w+)")


@pytest.mark.parametrize("doc", ["errors.md", "threat_model.md"])
def test_referenced_tests_exist(doc):
    refs = set(REF.findall((ROOT / doc).read_text(encoding="utf-8")))
    assert len(refs) >= 5
    missing = [f"{f}::{t}" for f, t in sorted(refs)
               if not re.search(rf"^def {t}\(", (ROOT / f).read_text(encoding="utf-8"), re.M)]
    assert missing == []


def test_error_taxonomy_covers_the_pdf_classes():
    text = (ROOT / "errors.md").read_text(encoding="utf-8").lower()
    for name in ["tool down", "bad arguments", "empty result", "wrong tool", "loop", "timeout", "user changes mind"]:
        assert f"**{name}**" in text, name


def test_threat_model_has_at_least_five_injection_cases():
    text = (ROOT / "threat_model.md").read_text(encoding="utf-8")
    assert len(set(re.findall(r"^\| (INJ-\d+b?) \|", text, re.M))) >= 5
