"""CR081 source-entry counts, local persistence receipts and unchanged policy."""
from __future__ import annotations

from inqaba import InqPath

import json
from pathlib import Path
import pytest

from umgodi import Pit
from umgodi.cli import main

ITEMS = [{"Id": "Alice", "Name": "Alice"}, {"Id": "Bob", "Name": "Bob"}, {"Id": "Carol", "Name": "Carol"}]


def seed(pit_test_root: Path, capsys, items, *options):
	source = pit_test_root / "source.json"
	source.write_text(json.dumps(items), encoding="utf-8")
	code = main(["seed", "Person", "--source", str(source), "-r", str(pit_test_root), "-n", *options])
	out = capsys.readouterr()
	return code, out.out, out.err


def fragments(pit_test_root: Path) -> int:
	with Pit.open(InqPath.from_directory(pit_test_root / "Person"), read_only=True, unflagged=True) as pit:
		return sum(len(history) for history in pit.historic_items.values())


@pytest.mark.parametrize("strict", [None, "-x", "--patch"])
def test_initial_repeat_mixed_counts_and_history(pit_test_root, capsys, strict):
	code, out, err = seed(pit_test_root, capsys, ITEMS)
	assert code == 0 and "3 source items: 3 written, 0 unchanged (Pit 'Person')." in out
	assert fragments(pit_test_root) == 3
	options = [strict] if strict else []
	code, out, err = seed(pit_test_root, capsys, ITEMS, *options, "--debug")
	assert code == 0 and "3 source items: 0 written, 3 unchanged (Pit 'Person')." in out
	assert 'Unchanged IDs: ["Alice","Bob","Carol"]' in err
	assert fragments(pit_test_root) == 3
	changed = [dict(item) for item in ITEMS]
	changed[0]["Name"] = "Alice revised"
	code, out, err = seed(pit_test_root, capsys, changed, *options)
	assert code == 0 and "3 source items: 1 written, 2 unchanged (Pit 'Person')." in out
	assert fragments(pit_test_root) == 4
	assert "Successfully committed" not in out


def test_duplicate_ids_are_source_entries(pit_test_root, capsys):
	code, out, _ = seed(pit_test_root, capsys, [{"Id": "Alice", "Name": "A"}, {"Id": "Alice", "Name": "A"}, {"Id": "Alice", "Name": "B"}])
	assert code == 0 and "3 source items: 2 written, 1 unchanged" in out
	assert fragments(pit_test_root) == 2


@pytest.mark.parametrize("flag", ["-x", "--patch"])
def test_strict_rejects_entire_batch_before_mutation(pit_test_root, capsys, flag):
	assert seed(pit_test_root, capsys, ITEMS)[0] == 0
	before = {str(p.relative_to(pit_test_root)): p.read_bytes() for p in (pit_test_root / "Person").rglob("*") if p.is_file()}
	code, out, err = seed(pit_test_root, capsys, [{"Id": "Alice", "Name": "Changed"}, {"Id": "Missing", "Name": "Bad"}], flag)
	assert code == 1 and "does not exist" in err
	assert "source items:" not in out
	after = {str(p.relative_to(pit_test_root)): p.read_bytes() for p in (pit_test_root / "Person").rglob("*") if p.is_file()}
	assert after == before


@pytest.mark.parametrize("stage", ["save", "dispose", "window"])
def test_persistence_failure_nonzero_without_receipt(pit_test_root, capsys, monkeypatch, stage):
	original_close = Pit.close
	if stage == "save":
		def fail_save(self, *args, **kwargs):
			raise OSError("simulated save failure")
		monkeypatch.setattr(Pit, "save", fail_save)
	elif stage == "dispose":
		def fail_close(self, *args, **kwargs):
			original_close(self, *args, **kwargs)
			raise OSError("simulated disposal failure")
		monkeypatch.setattr(Pit, "close", fail_close)
	else:
		monkeypatch.setattr(Pit, "try_release_process_window", lambda self: False)
	code, out, err = seed(pit_test_root, capsys, ITEMS)
	assert code == 1
	assert "source items:" not in out
	assert "failure" in err or "Could not release" in err


def test_receipt_follows_save_and_close(pit_test_root, capsys, monkeypatch):
	original_save, original_close = Pit.save, Pit.close
	order = []
	def save_checked(self, *args, **kwargs):
		assert "source items:" not in capsys.readouterr().out
		order.append("save")
		return original_save(self, *args, **kwargs)
	def close_checked(self, *args, **kwargs):
		assert "source items:" not in capsys.readouterr().out
		result = original_close(self, *args, **kwargs)
		order.append("close")
		return result
	monkeypatch.setattr(Pit, "save", save_checked)
	monkeypatch.setattr(Pit, "close", close_checked)
	code, out, _ = seed(pit_test_root, capsys, ITEMS)
	assert code == 0 and "3 source items: 3 written, 0 unchanged" in out
	assert order[-1] == "close" and "save" in order


def test_invalid_later_payload_before_open(pit_test_root, capsys):
	code, out, _ = seed(pit_test_root, capsys, [{"Id": "Alice"}, {"Id": "Bad", "Modified": "2020-01-01T00:00:00Z"}])
	assert code == 1 and "source items:" not in out
	assert not (pit_test_root / "Person").exists()


def test_retained_window_preserves_opt_out(pit_test_root, capsys):
	code, out, _ = seed(pit_test_root, capsys, ITEMS, "--window")
	assert code == 0 and "3 source items: 3 written, 0 unchanged" in out
	assert fragments(pit_test_root) == 3


@pytest.mark.parametrize("mode", ["TrackedChangesOnly", "TrackedChangesWithFallback"])
def test_latest_fragment_policy_and_live_identity_are_preserved(pit_test_root, mode):
	from umgodi import PitItem, MutationTrackingMode
	with Pit(InqPath.from_directory(pit_test_root / "policy"), "Policy", unflagged=True, autoload=False, tracking_mode=MutationTrackingMode(mode)) as pit:
		item = PitItem({"Id": "A", "Name": "same", "Other": "retained"})
		assert pit.add(item)
		assert pit["A"] is item
		stamp = item.modified
		assert not pit.add(PitItem({"Id": "A", "Name": "same", "Other": "retained"}))
		assert item.modified == stamp
		assert len(pit.historic_items["A"]) == 1
		# Same projected Name, different latest fragment: keep accepting this write.
		assert pit.add(PitItem({"Id": "A", "Name": "same"}))
		assert pit["A"] is item and len(pit.historic_items["A"]) == 2
		assert pit.add(PitItem({"Id": "A", "Other": None}))
		assert not pit.add(PitItem({"Id": "A", "Other": None}))
		assert "Other" not in pit["A"]
		assert len(pit.historic_items["A"]) == 3


def test_latest_comparison_preserves_json_types(pit_test_root):
	from umgodi import PitItem
	with Pit(InqPath.from_directory(pit_test_root / "types"), "Types", unflagged=True, autoload=False) as pit:
		assert pit.add(PitItem({"Id": "A", "Value": False}))
		assert pit.add(PitItem({"Id": "A", "Value": 0}))
		assert len(pit.historic_items["A"]) == 2


@pytest.mark.parametrize("first_engine", ["pits", "jpit"])
def test_cross_engine_seed_receipts_share_history(pit_test_root, first_engine):
	import os
	import subprocess
	import sys
	assembly = Path(__file__).resolve().parents[2] / "PitSeeder/pits/bin/Debug/net10.0/pits.dll"
	if not assembly.is_file():
		pytest.skip("Cross-engine CDAT requires the locally built pits candidate.")
	source = pit_test_root / "shared.json"
	commands = {
		"pits": ["dotnet", str(assembly), "add", "Person", "--source", str(source), "-r", str(pit_test_root) + "/", "-n"],
		"jpit": [sys.executable, "-m", "umgodi.cli", "seed", "Person", "--source", str(source), "-r", str(pit_test_root), "-n"],
	}
	second_engine = "jpit" if first_engine == "pits" else "pits"
	def run(engine, payload, expected, extra=()):
		source.write_text(json.dumps(payload), encoding="utf-8")
		result = subprocess.run([*commands[engine], *extra], capture_output=True, text=True, env=os.environ.copy())
		assert result.returncode == 0, (result.stdout, result.stderr)
		assert expected in result.stdout
		return result
	run(first_engine, ITEMS, "3 source items: 3 written, 0 unchanged")
	run(second_engine, ITEMS, "3 source items: 0 written, 3 unchanged")
	assert fragments(pit_test_root) == 3
	changed = [dict(item) for item in ITEMS]
	changed[0]["Name"] = "Cross-engine changed"
	run(second_engine, changed, "3 source items: 1 written, 2 unchanged", ("--patch",))
	assert fragments(pit_test_root) == 4
	run(first_engine, changed, "3 source items: 0 written, 3 unchanged", ("-x",))
	before = fragments(pit_test_root)
	for engine in (first_engine, second_engine):
		source.write_text(json.dumps([{"Id": "Alice", "Name": "must not land"}, {"Id": "Missing"}]), encoding="utf-8")
		result = subprocess.run([*commands[engine], "--patch"], capture_output=True, text=True)
		assert result.returncode == 1 and "does not exist" in result.stderr
		assert "source items:" not in result.stdout
		assert fragments(pit_test_root) == before
