from inqaba import InqPath
import datetime

import pytest

from umgodi.canonical import utcnow
from umgodi.changes import ChangeFile
from umgodi.flags import MasterTicket, get_machine_name
from umgodi.item import PitItem
from umgodi.store import Pit, PitSaveOutcome, PitSaveResult


@pytest.fixture
def pit(pit_test_root):
	directory = pit_test_root / "Tenant" / "Person"
	directory.mkdir(parents=True)
	(directory / "Person.pit").write_text("[]")
	instance = Pit(InqPath.from_directory(directory), "Person")
	yield instance
	instance.close(save_dirty=False)


@pytest.mark.parametrize(("age", "machine"), [
	(20, "Nkosikazi"), (70, "Mdlaka"), (90, "Mzansi"), (-90, "Remote"),
])
def test_returned_window(pit, age, machine):
	_, pit_sync, flag_sync, buffer = pit._load_timing()
	seconds = max(
		(flag_sync + buffer).total_seconds(),
		(pit_sync + buffer).total_seconds() - max(age, 0),
	)
	now = utcnow()
	pit.master_flag.write_ticket(MasterTicket("Returned", "remote-1", now, now - datetime.timedelta(seconds=age), machine + "-fixture"))
	acquired, ready = pit.try_acquire_master()
	assert acquired
	assert (ready - now).total_seconds() == pytest.approx(seconds, abs=.1)
	assert pit.try_acquire_master() == (False, None)


def test_read_and_legacy_held(pit):
	with pit:
		pit.load()
	assert not list(pit.pit_dir.glob("*.flag"))
	pit.master_flag.path.write_text(f"remote-app-1|{utcnow().isoformat()}")
	assert not pit.try_acquire_master()[0]


def test_collision_during_propagation(pit, monkeypatch):
	pit.add({"Id": "local"})
	def take_over(_delay):
		pit.master_flag.update(originator="other-app-1")
	monkeypatch.setattr("umgodi.store.time.sleep", take_over)
	assert pit.save() == PitSaveResult(PitSaveOutcome.PUBLISHED_AS_CHANGES, 1)
	assert pit.canonical_file.read_text() == "[]"
	assert len(list(pit.pit_dir.glob("*.json"))) == 1


def test_save_adopts_arriving_snapshot_and_returns(pit, monkeypatch):
	pit.add({"Id": "local"})
	def replicate(_delay):
		pit.canonical_file.write_text('[[{"Id":"remote"}]]')
		ChangeFile.create(pit.pit_dir, PitItem({"Id": "peer"}), "peer-app-1")
	monkeypatch.setattr("umgodi.store.time.sleep", replicate)
	pit.save()
	assert all(word in pit.canonical_file.read_text() for word in ('"peer"', '"remote"', '"local"'))
	assert pit.master_flag.ticket().state == "Returned"
	assert pit.master_flag.ticket().machine == get_machine_name()
	assert pit.try_acquire_master() == (False, None)
	pit.master_flag.path.unlink()
	assert pit.try_acquire_master() == (False, None)
	assert len(list(pit.pit_dir.glob("*.receipt"))) == 1


def test_expired_legacy_lease_ignores_recent_foreign_activity(pit):
	lease, pit_sync, flag_sync, buffer = pit._load_timing()
	now = utcnow()
	pit.master_flag.path.write_text(f"other-app-2|{(now - lease - datetime.timedelta(seconds=1)).isoformat()}")
	(pit.pit_dir / "other-app-2.flag").write_text(f"other:app:2|{now.isoformat()}")
	acquired, ready = pit.try_acquire_master()
	assert acquired
	assert (ready - now).total_seconds() == pytest.approx(max(pit_sync, flag_sync).total_seconds() + buffer.total_seconds(), abs=.1)


def test_sleep_past_expiry_writes_only_deltas(pit, monkeypatch):
	now = utcnow()
	lease, _, _, _ = pit._load_timing()
	pit.add({"Id": "after-suspend"})
	def wake(_delay):
		monkeypatch.setattr("umgodi.store.utcnow", lambda: now + lease + datetime.timedelta(seconds=1))
	monkeypatch.setattr("umgodi.store.time.sleep", wake)
	assert pit.save() == PitSaveResult(PitSaveOutcome.PUBLISHED_AS_CHANGES, 1)
	assert pit.canonical_file.read_text() == "[]"
	assert len(list(pit.pit_dir.glob("*.json"))) == 1
	assert len(list((pit.pit_dir / "Events").glob("*.event"))) == 1


def test_cli_list_and_export_leave_zero_flags(pit, capsys):
	from umgodi.cli import main

	root = str(pit.pit_dir.parent)
	pit.close(save_dirty=False)
	for args in (["list", "-r", root], ["export", "Person", "-r", root, "--json"]):
		assert main(args) == 0, capsys.readouterr().err
		assert not list(pit.pit_dir.glob("*.flag"))
		assert not (pit.pit_dir / "Events").exists()
