import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "esphome_versions", Path(__file__).resolve().parent.parent / "scripts" / "esphome_versions.py")
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)


def test_pin_from_parses_requirements():
    assert ev.pin_from(["foo==1", "aioesphomeapi==46.25.0", "bar"]) == "46.25.0"
    assert ev.pin_from(["aioesphomeapi == 46.12.0 ; python_version>'3.11'"]) == "46.12.0"


@pytest.mark.parametrize("bad", ["$(id)", "1.0;rm", "1.0`x`", "", "1.0 2.0", "a$b"])
def test_version_strings_are_validated(bad):
    with pytest.raises(SystemExit):
        ev.checked(bad)


def test_version_validation_accepts_pep440():
    for good in ["46.28.0", "2026.11.0", "2026.12.0b3", "1.0.post1", "1.0rc1"]:
        assert ev.checked(good) == good


def test_pinned_reads_requirements():
    assert ev.checked(ev.pinned())
