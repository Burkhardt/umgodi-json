"""Run the Umgodi test suite with pytest fixtures and parameterization."""

from pathlib import Path

import pytest


if __name__ == "__main__":
	raise SystemExit(pytest.main([str(Path(__file__).resolve().parent)]))
