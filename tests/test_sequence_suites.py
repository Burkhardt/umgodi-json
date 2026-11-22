"""
Discovers and executes all declarative cross-engine parity suites in tests/suites/.
"""

from __future__ import annotations

import shutil
import os
from pathlib import Path

import pytest

from tests.sequence_runner import SequenceRunner

SUITES_DIR = Path(__file__).resolve().parent / "suites"


def test_declarative_parity_suites(configured_test_cloud) -> None:
	"""Runs destructive cloud-backed parity suites only when explicitly requested."""
	if os.getenv("RAIKEEP_RUN_SEQUENCE_SUITES") != "1":
		pytest.skip("Set RAIKEEP_RUN_SEQUENCE_SUITES=1 to run parity suites")
	if not shutil.which("pits") or not shutil.which("jpit"):
		pytest.skip("Parity suites require pits and jpit on PATH")

	runner = SequenceRunner()
	for suite_file in sorted(SUITES_DIR.glob("*.json5")):
		res = runner.run_suite(suite_file)
		assert res.failed == 0, f"Suite '{res.name}' had {res.failed} failure(s)."
