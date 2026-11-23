"""
Unit tests for Pit maintenance and CLI parity (CR021/CR022).
Verifies report-only dry-runs, apply reconciliation, 10-minute receipt grace periods,
process flag pruning with --older-than, legacy event repairs, and JSON schema output.
"""

from __future__ import annotations

from inqaba import InqPath

import argparse
import datetime
import hashlib
import json
from pathlib import Path
import tempfile

import pytest

from umgodi.canonical import format_iso_timestamp, utcnow
from umgodi.changes import ChangeFile, ReceiptFile
from umgodi.cli import cmd_maintain, main, parse_duration
from umgodi.flags import MasterFlagFile, ProcessFlagFile, TimestampedValue
from umgodi.item import PitItem
from umgodi.store import Pit, PitMaintenanceOptions, PitMaintenanceResult
from tests.timing import wait_for_returned_ticket


def test_parse_duration() -> None:
	# Standard .NET TimeSpan formats
	assert parse_duration("01:00:00") == datetime.timedelta(hours=1)
	assert parse_duration("7.00:00:00") == datetime.timedelta(days=7)
	assert parse_duration("00:10:00") == datetime.timedelta(minutes=10)
	assert parse_duration("00:00:30") == datetime.timedelta(seconds=30)
	assert parse_duration("1:30:00") == datetime.timedelta(hours=1, minutes=30)

	# Suffix forms
	assert parse_duration("1h") == datetime.timedelta(hours=1)
	assert parse_duration("7d") == datetime.timedelta(days=7)
	assert parse_duration("10m") == datetime.timedelta(minutes=10)
	assert parse_duration("30s") == datetime.timedelta(seconds=30)

	# Raw integer seconds
	assert parse_duration("3600") == datetime.timedelta(seconds=3600)

	# Validation failures
	for invalid in ["", "0", "-1h", "00:00:00", "invalid", "xyz"]:
		try:
			parse_duration(invalid)
			assert False, f"Expected ValueError for '{invalid}'"
		except ValueError:
			pass


def test_maintain_dry_run_report_only(pit_test_root) -> None:
	with tempfile.TemporaryDirectory(dir=pit_test_root) as tmp:
		pit_dir = Path(tmp) / "TestPit"
		pit_dir.mkdir(parents=True)
		canonical = pit_dir / "TestPit.pit"
		canonical.write_text("[]\n", encoding="utf-8")

		# Create an expired process flag
		old_time = utcnow() - datetime.timedelta(hours=2)
		flag_file = pit_dir / "Host-Proc-1234.flag"
		flag_file.write_text(str(TimestampedValue("Host-Proc-1234", old_time)) + "\n", encoding="utf-8")

		# Create a pending change file using ChangeFile.create
		change_item = PitItem({"Id": "item-1", "Name": "DryRun"})
		cf_file = ChangeFile.create(pit_dir, change_item, "Host-Proc-1234")

		with Pit.open(InqPath.from_directory(pit_dir), read_only=True, unflagged=True) as pit:
			res = pit.maintain(apply=False)

		assert res.applied is False
		assert res.succeeded is True
		assert res.change_files_observed == 1
		assert res.change_files_merged == 1
		assert res.change_files_removed == 0
		assert res.process_flags_expired == 1
		assert res.process_flags_pruned == 0

		# On disk, nothing was removed or modified
		assert cf_file.is_file()
		assert flag_file.is_file()
		assert canonical.read_text(encoding="utf-8") == "[]\n"


def test_maintain_apply_reconciliation_and_receipt_grace(pit_test_root) -> None:
	with tempfile.TemporaryDirectory(dir=pit_test_root) as tmp:
		pit_dir = Path(tmp) / "TestPit"
		pit_dir.mkdir(parents=True)
		canonical = pit_dir / "TestPit.pit"
		canonical.write_text("[]\n", encoding="utf-8")

		change_item = PitItem({"Id": "item-abc", "Status": "Active"})
		cf_file = ChangeFile.create(pit_dir, change_item, "Host-Proc-5555")

		# First apply: merges change, persists canonical, creates receipt
		with Pit.open(InqPath.from_directory(pit_dir), unflagged=True, autoload=False) as pit:
			res1 = pit.maintain(apply=True)

		assert res1.applied is True
		assert res1.canonical_persisted is True
		assert res1.receipts_created == 1
		assert res1.change_files_removed == 0  # Grace period active

		receipt_file = cf_file.with_suffix(".receipt")
		assert receipt_file.is_file()
		assert cf_file.is_file()

		# Backdate receipt past the 10-minute grace period
		past_time = utcnow() - datetime.timedelta(minutes=11)
		receipt_file.write_text(f"{format_iso_timestamp(past_time)}\n", encoding="utf-8")

		# Second apply: grace period expired, both change file and receipt pruned
		with Pit.open(InqPath.from_directory(pit_dir), unflagged=True, autoload=False) as pit:
			res2 = pit.maintain(apply=True)

		assert res2.applied is True
		assert res2.change_files_removed == 1
		assert res2.receipts_removed == 1
		assert not cf_file.exists()
		assert not receipt_file.exists()


def test_maintain_prune_process_flags(pit_test_root) -> None:
	with tempfile.TemporaryDirectory(dir=pit_test_root) as tmp:
		pit_dir = Path(tmp) / "TestPit"
		pit_dir.mkdir(parents=True)
		canonical = pit_dir / "TestPit.pit"
		canonical.write_text("[]\n", encoding="utf-8")

		now = utcnow()

		# 1. Master.flag (must never be pruned)
		m_flag = pit_dir / "Master.flag"
		m_flag.write_text(str(TimestampedValue("MasterHost", now)) + "\n", encoding="utf-8")

		# 2. Active process flag (now - 10s)
		active_flag = pit_dir / "Host-Proc-1111.flag"
		active_flag.write_text(str(TimestampedValue("Host:Proc:1111", now - datetime.timedelta(seconds=10))) + "\n", encoding="utf-8")

		# 3. Expired process flag younger than 1h (now - 15m)
		young_expired = pit_dir / "Host-Proc-2222.flag"
		young_expired.write_text(str(TimestampedValue("Host:Proc:2222", now - datetime.timedelta(minutes=15))) + "\n", encoding="utf-8")

		# A PID mismatch cannot authorize deletion, regardless of age.
		mismatch = pit_dir / "Host-Proc-4444.flag"
		mismatch.write_text(str(TimestampedValue("Host:Proc:5555", now - datetime.timedelta(hours=2))))

		# 4. Expired process flag older than 1h (now - 2h)
		old_expired = pit_dir / "Host-Proc-3333.flag"
		old_expired.write_text(str(TimestampedValue("Host:Proc:3333", now - datetime.timedelta(hours=2))) + "\n", encoding="utf-8")

		opts = PitMaintenanceOptions(
			apply=True,
			prune_process_flags=True,
			older_than=datetime.timedelta(hours=1),
		)

		with Pit.open(InqPath.from_directory(pit_dir), unflagged=True) as pit:
			res = pit.maintain(opts)

		assert res.process_flags_active == 1
		assert mismatch.exists()
		assert res.process_flags_expired == 3
		assert res.process_flags_pruned == 1
		assert res.succeeded is True

		assert m_flag.is_file()
		assert active_flag.is_file()
		assert young_expired.is_file()
		assert not old_expired.exists()


def test_maintain_repair_legacy_events(pit_test_root) -> None:
	with tempfile.TemporaryDirectory(dir=pit_test_root) as tmp:
		pit_dir = Path(tmp) / "TestPit"
		pit_dir.mkdir(parents=True)
		(pit_dir / "TestPit.pit").write_text("[]\n", encoding="utf-8")

		events_dir = pit_dir / "Events"
		events_dir.mkdir()

		body = "{\"Event\": \"test\"}\n"
		digest = hashlib.sha256(body.encode("utf-8")).hexdigest()

		legacy_file = events_dir / f"EventStem_{digest}.event"
		legacy_file.write_text(body, encoding="utf-8")

		opts = PitMaintenanceOptions(apply=True, repair_legacy_extensions=True)

		with Pit.open(InqPath.from_directory(pit_dir), unflagged=True) as pit:
			res = pit.maintain(opts)

		assert res.legacy_artifacts_observed == 1
		assert res.legacy_artifacts_repaired == 1

		clean_file = events_dir / "EventStem.event"
		assert clean_file.is_file()
		assert not legacy_file.exists()


def test_maintain_cli_validation_rules() -> None:
	# --prune-process-flags requires --apply
	args1 = argparse.Namespace(
		pit="Dummy",
		apply=False,
		prune_process_flags=True,
		older_than="01:00:00",
		repair_legacy_extensions=False,
		archive_events=False,
		json=False,
		wwwa=False,
	)
	assert cmd_maintain(args1) == 1

	# --prune-process-flags requires --older-than
	args2 = argparse.Namespace(
		pit="Dummy",
		apply=True,
		prune_process_flags=True,
		older_than=None,
		repair_legacy_extensions=False,
		archive_events=False,
		json=False,
		wwwa=False,
	)
	assert cmd_maintain(args2) == 1

	# --older-than applies only with --prune-process-flags
	args3 = argparse.Namespace(
		pit="Dummy",
		apply=True,
		prune_process_flags=False,
		older_than="01:00:00",
		repair_legacy_extensions=False,
		archive_events=False,
		json=False,
		wwwa=False,
	)
	assert cmd_maintain(args3) == 1

	# --repair-legacy-extensions requires --apply
	args4 = argparse.Namespace(
		pit="Dummy",
		apply=False,
		prune_process_flags=False,
		older_than=None,
		repair_legacy_extensions=True,
		archive_events=False,
		json=False,
		wwwa=False,
	)
	assert cmd_maintain(args4) == 1

	# both <pit> and --wwwa
	args5 = argparse.Namespace(
		pit="Dummy",
		wwwa=True,
		apply=True,
		prune_process_flags=False,
		older_than=None,
		repair_legacy_extensions=False,
		archive_events=False,
		json=False,
	)
	assert cmd_maintain(args5) == 1


def test_maintain_cli_json_schema(pit_test_root) -> None:
	with tempfile.TemporaryDirectory(dir=pit_test_root) as tmp:
		pit_dir = Path(tmp) / "TestPit"
		pit_dir.mkdir(parents=True)
		canonical = pit_dir / "TestPit.pit"
		canonical.write_text("[]\n", encoding="utf-8")

		args = argparse.Namespace(
			pit=str(pit_dir),
			apply=False,
			prune_process_flags=False,
			older_than=None,
			repair_legacy_extensions=False,
			archive_events=False,
			json=True,
			wwwa=False,
			cloud="OneDrive",
			root=None,
		)

		import io
		import sys

		buf = io.StringIO()
		old_stdout = sys.stdout
		try:
			sys.stdout = buf
			rc = cmd_maintain(args)
		finally:
			sys.stdout = old_stdout

		assert rc == 0
		data = json.loads(buf.getvalue())

		expected_keys = {
			"PitFile", "Applied", "Succeeded", "MasterFlagsObserved",
			"ConflictFlagsObserved", "ProcessFlagsActive", "ProcessFlagsExpired",
			"ProcessFlagsPruned", "ProcessFlagsMalformed", "ChangeFilesObserved",
			"ChangeFilesInvalid", "ChangeFilesValid", "ChangeFilesMerged",
			"ChangeFilesEligible", "ChangeFilesRemoved", "ReceiptsObserved",
			"ReceiptsCreated", "ReceiptsRetained", "ReceiptsRemoved",
			"ReceiptsMalformed", "LegacyArtifactsObserved", "LegacyArtifactsRepaired",
			"EventFilesObserved", "EventFilesArchived", "EventFilesRemoved",
			"EventArchiveName", "CanonicalPersisted", "CurrentMaster",
			"Deferred", "Failures"
		}
		for key in expected_keys:
			assert key in data, f"Key '{key}' missing from maintain JSON output"


def test_maintain_wwwa_cloud_not_hijacked_by_local_cwd_directory(cloud_tenant, tmp_path, monkeypatch, capsys):
	provider, tenant = cloud_tenant
	monkeypatch.chdir(tmp_path)
	(tmp_path / tenant.name).mkdir()
	for pit in ("Activity", "Object", "Person", "Place"):
		directory = tenant / pit
		directory.mkdir()
		(directory / f"{pit}.pit").write_text("[]", encoding="utf-8")
	args = argparse.Namespace(pit=None, wwwa=True, cloud=provider, root=tenant.name,
		json=True, machine="all", level="Trace", apply=False, prune_process_flags=False,
		older_than=None, repair_legacy_extensions=False, archive_events=False)
	assert cmd_maintain(args) == 0
	rows = json.loads(capsys.readouterr().out)
	assert isinstance(rows, list)
	assert len(rows) == 4
	assert all(Path(row["PitFile"]).is_relative_to(tenant) for row in rows)



@pytest.fixture
def realistic_tenant(pit_test_root):
	tenant = pit_test_root / "Tenant"
	tenant.mkdir()
	return tenant


@pytest.mark.parametrize("age", [datetime.timedelta(minutes=5), datetime.timedelta(days=8)])
def test_historical_save_publishes_current_lease_without_changing_history(realistic_tenant, age):
	directory = realistic_tenant / "HistoricalSave"
	directory.mkdir()
	old = utcnow() - age
	with Pit.open(InqPath.from_directory(directory), autoload=False) as pit:
		pit.add_historical(PitItem({"Id": "Past", "Modified": format_iso_timestamp(old)}, invalidate=False))
		reader_checkpoint = utcnow() - datetime.timedelta(seconds=2)
		before_save = utcnow()
		pit.save(force=True)

		master = MasterFlagFile(directory)
		assert master.ticket().owner == pit.exact_process_identity
		assert master.ticket().state == "Returned"
		assert before_save <= master.time <= utcnow()
		assert master.ticket().written >= master.time
		assert master.time > reader_checkpoint > old
		assert pit["Past"].modified == old
		data = json.loads(pit.canonical_file.read_text())
		assert data[0][0]["Modified"] == format_iso_timestamp(old)


def test_jpit_maintain_historical_store_retires_elapsed_receipt(realistic_tenant, capsys):
	directory = realistic_tenant / "HistoricalCli"
	directory.mkdir()
	old = format_iso_timestamp(utcnow() - datetime.timedelta(days=8))
	first = PitItem({"Id": "First", "Modified": old}, invalidate=False)
	change = ChangeFile.create(directory, first, "Peer-app-42")
	with Pit.open(InqPath.from_directory(directory), unflagged=True, autoload=False) as fixture:
		assert fixture.maintain(apply=True).receipts_created == 1
		receipt = change.with_suffix(".receipt")
		receipt.write_text(format_iso_timestamp(utcnow() - datetime.timedelta(minutes=11)))
		second = PitItem({"Id": "Second", "Modified": old}, invalidate=False)
		pending = ChangeFile.create(directory, second, "Peer-app-42")

	assert main(["maintain", "HistoricalCli", "-r", str(realistic_tenant), "--apply", "--json"]) == 0
	result = json.loads(capsys.readouterr().out)
	assert result["CurrentMaster"] and result["CanonicalPersisted"]
	assert result["ChangeFilesRemoved"] == result["ReceiptsRemoved"] == 1
	assert result["Deferred"] == result["Failures"] == []
	assert not change.exists() and not receipt.exists()
	assert pending.is_file() and pending.with_suffix(".receipt").is_file()
	assert MasterFlagFile(directory).ticket().state == "Returned"
	data = json.loads((directory / "HistoricalCli.pit").read_text())
	assert {fragment["Id"] for history in data for fragment in history} == {"First", "Second"}
	assert all(fragment["Modified"] == old for history in data for fragment in history)


def test_historical_store_renews_lease_and_retires_accounted_changes(realistic_tenant) -> None:
	directory = realistic_tenant / "Historical"
	directory.mkdir()
	old = format_iso_timestamp(utcnow() - datetime.timedelta(days=8))

	def publish(key):
		item = PitItem({"Id": key, "Modified": old}, invalidate=False)
		return ChangeFile.create(directory, item, "Peer-app-42")

	change = publish("First")
	with Pit.open(InqPath.from_directory(directory), autoload=False) as pit:
		assert pit.maintain(apply=True).canonical_persisted
		assert pit.master_flag.ticket().state == "Returned"
		assert pit.master_flag.ticket().written >= pit.master_flag.time
		receipt = change.with_suffix(".receipt")
		receipt.write_text(format_iso_timestamp(utcnow() - datetime.timedelta(minutes=11)))
		pending = publish("Second")
		wait_for_returned_ticket(pit)
		result = pit.maintain(apply=True)
		assert result.canonical_persisted and result.deferred == []
		assert result.change_files_removed == result.receipts_removed == 1
		assert not change.exists() and not receipt.exists()
		assert pending.is_file()
		assert pit.master_flag.ticket().state == "Returned"
		data = json.loads(pit.canonical_file.read_text())
		assert {frag["Id"] for history in data for frag in history} == {"First", "Second"}


def test_maintenance_rechecks_owner_and_canonical_evidence_before_cleanup(realistic_tenant, monkeypatch):
	for failure in ("foreign-owner", "missing-history"):
		directory = realistic_tenant / failure
		directory.mkdir()
		change = ChangeFile.create(directory, PitItem({"Id": "Old"}), "Peer-app-42")
		with Pit.open(InqPath.from_directory(directory), autoload=False) as pit:
			assert pit.maintain(apply=True).receipts_created == 1
			receipt = change.with_suffix(".receipt")
			receipt.write_text(format_iso_timestamp(utcnow() - datetime.timedelta(minutes=11)))
			ChangeFile.create(directory, PitItem({"Id": "NeedsStore"}), "Peer-app-42")
			store = pit._store_canonical

			def store_then_intervene(**kwargs):
				written = store(**kwargs)
				if failure == "foreign-owner":
					pit.master_flag.update(originator="OtherHost-peer-42")
				else:
					pit.canonical_file.write_text("[]")
				return written

			monkeypatch.setattr(pit, "_store_canonical", store_then_intervene)
			wait_for_returned_ticket(pit)
			result = pit.maintain(apply=True)
			assert result.change_files_removed == result.receipts_removed == 0
			assert change.is_file() and receipt.is_file()
			if failure == "foreign-owner":
				assert f"Cleanup revalidation deferred {change.name}" in result.deferred
				assert pit.master_flag.is_owned_by("OtherHost-peer-42")
			else:
				assert f"Canonical pit does not yet account for {change.name}" in result.deferred


def test_writable_open_preserves_backlog_until_maintenance(realistic_tenant):
	directory = realistic_tenant / "Backlog"
	directory.mkdir()
	old = utcnow() - datetime.timedelta(days=2)
	eligible = format_iso_timestamp(utcnow() - datetime.timedelta(minutes=11))
	data, changes = [], []
	for index in range(102):
		item = PitItem({"Id": f"Item{index}", "Modified": format_iso_timestamp(old + datetime.timedelta(seconds=index))}, invalidate=False)
		data.append([item.to_dict()])
		change = ChangeFile.create(directory, item, "Peer-app-42")
		changes.append(change)
		if index < 101:
			change.with_suffix(".receipt").write_text(eligible if index < 100 else format_iso_timestamp(utcnow()))
	canonical = directory / "Backlog.pit"
	canonical.write_text(json.dumps(data))
	last_write = canonical.stat().st_mtime_ns
	with Pit.open(InqPath.from_directory(directory), read_only=True, unflagged=True):
		assert all(change.is_file() for change in changes)
	with Pit.open(InqPath.from_directory(directory)) as opened:
		assert len(opened) == 102
		assert canonical.stat().st_mtime_ns == last_write
		assert all(p.exists() for p in changes)
		assert opened.maintain(apply=True).change_files_removed == 100
		assert all(not p.exists() and not p.with_suffix(".receipt").exists() for p in changes[:100])
		assert all(p.is_file() and p.with_suffix(".receipt").is_file() for p in changes[100:])


def test_jpit_maintain_apply_respects_foreign_lease(realistic_tenant, capsys):
	directory = realistic_tenant / "Busy"
	directory.mkdir()
	item = PitItem({"Id": "Canonical"})
	(directory / "Busy.pit").write_text(json.dumps([[item.to_dict()]]))
	change = ChangeFile.create(directory, item, "Peer-app-42")
	receipt = change.with_suffix(".receipt")
	receipt.write_text(format_iso_timestamp(utcnow() - datetime.timedelta(minutes=11)))
	foreign = MasterFlagFile(directory)
	foreign.update(originator="Peer-app-42")
	assert main(["maintain", "Busy", "-r", str(realistic_tenant), "--apply", "--json"]) == 0
	result = json.loads(capsys.readouterr().out)
	assert not result["CurrentMaster"]
	assert result["ChangeFilesRemoved"] == result["ReceiptsRemoved"] == 0
	assert foreign.is_owned_by("Peer-app-42") and change.is_file() and receipt.is_file()


def test_jpit_seed_ordinary_open_retires_accounted_change(realistic_tenant):
	directory = realistic_tenant / "Person"
	directory.mkdir()
	item = PitItem({"Id": "Imported"})
	(directory / "Person.pit").write_text(json.dumps([[item.to_dict()]]))
	change = ChangeFile.create(directory, item, "Peer-app-42")
	receipt = change.with_suffix(".receipt")
	receipt.write_text(format_iso_timestamp(utcnow() - datetime.timedelta(minutes=11)))
	source = realistic_tenant / "new-person.json"
	source.write_text('{"Id":"NewEntry"}')
	assert main(["seed", "Person", "--source", str(source), "-r", str(realistic_tenant), "-n"]) == 0
	assert not change.exists() and not receipt.exists()
