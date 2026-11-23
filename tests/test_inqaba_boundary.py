from inqaba import InqPath
import ast
from pathlib import Path

from inqaba import InqConfig, InqFile, InqPath
from umgodi import Pit
from umgodi.config import OsConfig
from umgodi.fs import safe_write_in_place, safe_write_sibling_tmp


def test_store_uses_inqaba_paths(pit_test_root):
	with Pit(InqPath.from_directory(pit_test_root), "Example", read_only=False, unflagged=True) as pit:
		assert isinstance(pit.pit_dir, InqPath)
		assert isinstance(pit.canonical_file, InqFile)
	assert OsConfig is InqConfig


def test_write_contracts_survive_boundary_migration(pit_test_root):
	file = InqFile(pit_test_root / "Example.pit")
	safe_write_in_place(file, "first")
	inode = file.stat().st_ino
	safe_write_in_place(file, "second", backup=True, backup_dir=InqPath(pit_test_root / "backups"))
	assert file.stat().st_ino == inode
	assert (pit_test_root / "backups/Example.pit.bak").read_text() == "first"
	safe_write_sibling_tmp(file, "third")
	assert file.read_text() == "third"
	assert not (pit_test_root / "Example.pit.tmp").exists()


def test_runtime_does_not_import_pathlib_or_call_raw_open():
	for source in (Path(__file__).parents[1] / "umgodi").glob("*.py"):
		for node in ast.walk(ast.parse(source.read_text())):
			assert not (isinstance(node, ast.ImportFrom) and node.module == "pathlib"), source
			assert not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open"), source
