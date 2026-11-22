import asyncio

import pytest

from umgodi import Pit, PitSaveOutcome, PitSaveResult


def test_canonical_write_and_clean_save(tmp_path):
	with Pit.open(tmp_path / "Person", unflagged=True) as pit:
		pit.add({"Id": "X1"})
		assert asyncio.run(pit.save_async()) == PitSaveResult(PitSaveOutcome.WROTE)
		assert not pit.invalid()
		content = pit.canonical_file.read_bytes()
		stamp = pit.canonical_file.stat().st_mtime_ns
		assert b'"X1"' in content
		assert pit.save() == PitSaveResult(PitSaveOutcome.NOTHING_TO_WRITE)
		assert pit.canonical_file.read_bytes() == content
		assert pit.canonical_file.stat().st_mtime_ns == stamp


def test_foreign_lease_reports_only_published_fragments(tmp_path):
	directory = tmp_path / "Person"
	directory.mkdir()
	canonical = directory / "Person.pit"
	canonical.write_text("[]")
	stamp = canonical.stat().st_mtime_ns
	with Pit.open(directory) as pit:
		pit.master_flag.update(originator="Remote-app-123")
		assert pit.save(force=True) == PitSaveResult(PitSaveOutcome.NOTHING_TO_WRITE)
		assert not list(directory.glob("*.json"))
		for item_id in ("X1", "X2"):
			pit.add({"Id": item_id})
		assert asyncio.run(pit.save_async()) == PitSaveResult(PitSaveOutcome.PUBLISHED_AS_CHANGES, 2)
		assert len(list(directory.glob("*.json"))) == 2
		assert not pit.invalid()
		assert canonical.read_text() == "[]"
		assert canonical.stat().st_mtime_ns == stamp


def test_explicit_load_and_read_only(tmp_path):
	directory = tmp_path / "Person"
	directory.mkdir()
	(directory / "Person.pit").write_text("[]")
	with Pit.open(directory, autoload=False) as pit:
		assert pit.save() == PitSaveResult(PitSaveOutcome.PIT_NOT_LOADED)
		assert pit.load()
		assert pit.save() == PitSaveResult(PitSaveOutcome.NOTHING_TO_WRITE)
	with Pit.open(directory, read_only=True) as pit:
		with pytest.raises(OSError, match="read-only"):
			pit.save()
		with pytest.raises(OSError, match="read-only"):
			asyncio.run(pit.save_async())
	assert not list(directory.glob("*.flag"))
