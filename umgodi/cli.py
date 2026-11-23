"""
Command-line interface for umgodi and jpit.
Provides living-state semantic search (Pit-Grep), stream piping with jq,
point-in-time time-travel, and JSON5 mutation ingestion.
"""

from __future__ import annotations

import argparse
import copy
import datetime
import json
import os
import platform
from inqaba import InqEntry, InqPath, InqRelPath
import re
import subprocess
import sys
from typing import Any

from .audit import LogLevel, PitAudit, PitAuditEvent
from .canonical import format_iso_timestamp, parse_iso_timestamp, utcnow
from .config import InqConfig, missing_configuration_diagnostic
from .exceptions import (
	JsonPitError,
	PitNotFoundError,
	ProtectedAttributeError,
)
from .fs import is_explicit_path, resolve_command_pit_root, resolve_pit_target
from .icons import Icons
from . import cli_style as help_style
from .store import (
	Pit,
	PitItem,
	PitMaintenanceOptions,
	PitMaintenanceResult,
	PitSaveOutcome,
	parse_and_validate_seed_payload,
)
from . import __version__


def run_jq_filter(json_text: str, jq_filter: str) -> int:
	"""Pass JSON to jq and propagate its exit status."""
	try:
		result = subprocess.run(["jq", jq_filter], input=json_text, text=True, capture_output=True)
	except FileNotFoundError:
		sys.stderr.write("error: jq executable not found on PATH.\n")
		return 1
	sys.stdout.write(result.stdout)
	sys.stderr.write(result.stderr)
	return result.returncode


def discover_pits(
	target: str,
	cloud: str | None = None,
	root: str | None = None,
) -> list[tuple[InqPath, str]]:
	"""
	Discovers one or multiple pits based on a target name or root.
	If target is an entire root (e.g. 'AIA'), discovers all contained pits.
	"""
	if target in (".", ""):
		directory = resolve_command_pit_root(root=root, cloud=cloud, operation="grep") if root or cloud else InqEntry.cwd()
	else:
		target_dir, name = resolve_pit_target(target, root=root, cloud=cloud)
		if (target_dir / f"{name}.pit").is_file():
			return [(target_dir, name)]
		directory = target_dir
	if not directory.is_dir():
		return []
	pits = []
	for child in sorted(directory.iterdir()):
		if child.is_file() and child.suffix.lower() == ".pit":
			pits.append((InqPath.from_directory(child.parent), child.stem))
		elif child.is_dir() and (child / f"{child.name}.pit").is_file():
			pits.append((InqPath.from_directory(child), child.name))
	return pits


def _open_cli_pit(target: str, cloud: str | None, root: str | None, **options: Any) -> Pit:
	pit_dir, pit_name = resolve_pit_target(target, cloud=cloud, root=root)
	return Pit(pit_dir, pit_name, **options)


def extract_cloud_and_root(argv: list[str]) -> tuple[str | None, str | None]:
	"""Extracts -c/--cloud and -r/--root options from CLI token list."""
	cloud: str | None = None
	root = None
	i = 0
	while i < len(argv):
		arg = argv[i]
		if arg in ("-c", "--cloud", "--cloudprovider") and i + 1 < len(argv):
			cloud = argv[i + 1]
			i += 2
		elif arg.startswith(("--cloud=", "--cloudprovider=")):
			cloud = arg.split("=", 1)[1]
			i += 1
		elif arg in ("-r", "--pitroot", "--root") and i + 1 < len(argv):
			root = argv[i + 1]
			i += 2
		elif arg.startswith(("--pitroot=", "--root=")):
			root = arg.split("=", 1)[1]
			i += 1
		else:
			i += 1
	return cloud, root


def expand_list_flag_bundles(argv: list[str]) -> list[str]:
	"""Expands the supported POSIX list bundles without changing other commands."""
	if not any(argument in ("list", "ls") for argument in argv):
		return argv
	expanded: list[str] = []
	for argument in argv:
		if argument in ("-la", "-al"):
			expanded.extend(["-l", "-a"])
		else:
			expanded.append(argument)
	return expanded


def cmd_grep(args: argparse.Namespace) -> int:
	"""
	Pit-Grep: Living-state semantic search across single or multiple pits.
	Filters out deleted entities by default, supports time-travel and property scoping.
	"""
	pattern_raw = args.pattern
	target_raw = args.target

	at_time: datetime.datetime | None = None
	if args.at:
		if not re.search(r"(?:Z|[+-]\d{2}:\d{2})$", args.at, re.IGNORECASE):
			sys.stderr.write("error: --at requires an offset-explicit ISO-8601 timestamp.\n")
			return 1
		at_time = parse_iso_timestamp(args.at)

	flags = re.IGNORECASE if args.ignore_case else 0
	try:
		pattern = re.compile(pattern_raw if args.regex else re.escape(pattern_raw), flags)
	except re.error as ex:
		sys.stderr.write(f"[jpit] Regex error: {ex}\n")
		return 1

	pits = discover_pits(target_raw, cloud=args.cloud, root=args.root)
	if not pits:
		sys.stderr.write(f"[jpit] No pits found for target '{target_raw}'.\n")
		return 1

	matched_entities: list[dict[str, Any]] = []

	for pit_dir, pit_name in pits:
		try:
			with Pit(
				pit_dir=pit_dir,
				pit_name=pit_name,
				read_only=True,
				unflagged=True,
			) as pit:
				for key in sorted(pit.historic_items):
					item = pit.get(key, at=at_time, with_deleted=args.with_deleted)
					if item is None:
						continue
					item_dict = item.to_dict()

					values = [_get_nested(item_dict, args.property)] if args.property else item_dict.values()
					if not any(pattern.search(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, separators=(",", ":")))
							for v in values if v is not None):
						continue
					record = item_dict | {"_pit": pit_name}
					matched_entities.append(record)
					if not (args.json or args.jq):
						print(f"[{pit_name} : {key}] {record.get('Name') or key}")
		except Exception as ex:
			sys.stderr.write(f"[jpit] Error reading pit {pit_name}: {ex}\n")
			return 1

	if args.json or args.jq:
		raw_json = json.dumps(matched_entities, indent=2, ensure_ascii=False)
		if args.jq:
			return run_jq_filter(raw_json, args.jq)
		else:
			sys.stdout.write(raw_json + "\n")

	return 0


def _get_nested(data: dict[str, Any], path: str) -> Any:
	"""Navigates dot-delimited path in a dictionary."""
	curr: Any = data
	for part in path.split("."):
		if isinstance(curr, dict):
			curr = curr.get(part)
		else:
			return None
	return curr


def cmd_history(args: argparse.Namespace) -> int:
	"""Dumps the raw immutable fragment history array for an entity."""
	with _open_cli_pit(
		args.pit,
		cloud=args.cloud,
		root=args.root,
		read_only=True,
		unflagged=True,
	) as pit:
		if not pit.canonical_file.is_file():
			sys.stderr.write(f"[jpit] Pit '{args.pit}' not found at {pit.pit_dir}.\n")
			return 1

		pit_items = pit._historic_items.get(args.id)
		if pit_items is None:
			sys.stderr.write(f"[jpit] No history for '{args.id}' in Pit '{pit.pit_name}'.\n")
			return 1

		fragments = [frag.to_dict() for frag in pit_items.history]
		output = json.dumps(fragments, indent=2, ensure_ascii=False)
		if args.jq:
			return run_jq_filter(output, args.jq)
		else:
			sys.stdout.write(output + "\n")
	return 0


def _discover_pit_names(directory: InqEntry) -> list[str]:
	if not directory.is_dir():
		return []
	return sorted({
		child.name
		for child in directory.iterdir()
		if child.is_dir() and (child / f"{child.name}.pit").is_file()
	} | {
		file.stem for file in directory.glob("*.pit") if file.is_file()
	})


def _format_pit_listing(directory: InqEntry, name: str, long_listing: bool) -> str:
	if not long_listing:
		return f"  {name}"
	nested = directory / name / f"{name}.pit"
	flat = directory / f"{name}.pit"
	metadata = (nested if nested.is_file() else flat).stat()
	size = f"{metadata.st_size / 1024:.1f} KB"
	modified = datetime.datetime.fromtimestamp(metadata.st_mtime).strftime("%Y-%m-%d %H:%M")
	return f"  {name:<12}  {size:>9}   {modified}"


def _format_discovery_directory(directory: InqEntry) -> str:
	"""Matches pits' RaiPath display form, including its terminal separator."""
	text = str(directory)
	return text if text.endswith(os.sep) else text + os.sep


def _render_discovery(
	provider: str | None,
	directory: InqEntry,
	pits: list[str],
	all_clouds: bool,
	long_listing: bool = False,
) -> str:
	display_directory = _format_discovery_directory(directory)
	if all_clouds:
		if not pits:
			return f"[{provider}] ({display_directory}): No pits found."
		heading = f"[{provider}] ({display_directory}): {len(pits)} pit(s) found"
	else:
		heading = f"Found {len(pits)} pit(s) in cloud '{provider}' ({display_directory}):"
	return "\n".join([heading, *[_format_pit_listing(directory, name, long_listing) for name in pits]])


def _discover_tenant_roots(root: str, cloud: str | None, all_clouds: bool) -> list[tuple[str | None, InqEntry, list[str]]]:
	"""Discovers a local root or configured cloud tenant roots without guessing a provider."""
	if cloud:
		candidate = resolve_command_pit_root(root=root, cloud=cloud, target_name=root, operation="list")
		return [(cloud, candidate, _discover_pit_names(candidate))]
	if is_explicit_path(root):
		candidate = InqPath.from_directory(root)
		return [(None, candidate, _discover_pit_names(candidate))]

	cfg = InqConfig.load()
	if not cfg.is_config_loaded:
		raise PitNotFoundError(missing_configuration_diagnostic())
	providers = [provider for provider in cfg.default_cloud_order if cfg.get_cloud_root(provider) is not None]
	if not providers:
		raise PitNotFoundError("No configured DefaultCloudOrder providers are available for tenant discovery.")
	discoveries = [
		(provider, cfg.get_cloud_root(provider) / root, _discover_pit_names(cfg.get_cloud_root(provider) / root))
		for provider in providers
	]
	if all_clouds:
		return discoveries
	return [next((discovery for discovery in discoveries if discovery[2]), discoveries[0])]


def _require_explicit_mutation_location(args: argparse.Namespace) -> None:
	"""Prevents mutations from silently selecting a provider from DefaultCloudOrder."""
	if getattr(args, "cloud", None):
		return
	root = getattr(args, "root", None)
	if root and is_explicit_path(root):
		return
	pit = getattr(args, "pit", None)
	if pit and is_explicit_path(str(pit)):
		return
	label = root or pit or "the requested Pit"
	raise JsonPitError(
		f"Mutation refuses to guess a cloud for '{label}'. Supply -c <provider> or an explicit local directory path."
	)


def cmd_list_discovery(args: argparse.Namespace) -> int:
	"""Lists tenant pits with mandatory cloud and directory provenance."""
	root = getattr(args, "root", None)
	cloud = getattr(args, "cloud", None)
	all_clouds = bool(getattr(args, "all", False))
	long_listing = bool(getattr(args, "long", False))
	if not root:
		raise JsonPitError("list requires -r or --root <tenant-or-path>.")
	if all_clouds and cloud:
		raise JsonPitError("--all scans DefaultCloudOrder and cannot be combined with -c or --cloud.")

	discoveries = _discover_tenant_roots(root, cloud, all_clouds)
	if all_clouds:
		for provider, directory, pits in discoveries:
			sys.stdout.write(_render_discovery(provider, directory, pits, all_clouds=True, long_listing=long_listing) + "\n")
		return 0

	provider, directory, pits = discoveries[0]
	if provider is None:
		sys.stdout.write(f"Found {len(pits)} pit(s) in local directory '{_format_discovery_directory(directory)}':\n")
		for name in pits:
			sys.stdout.write(_format_pit_listing(directory, name, long_listing) + "\n")
		return 0
	if not pits and cloud is None:
		sys.stdout.write(f"No pits found under tenant root '{root}' across configured clouds.\n")
		return 0
	sys.stdout.write(_render_discovery(provider, directory, pits, all_clouds=False, long_listing=long_listing) + "\n")
	return 0


def _save_mutation(pit: Pit, debug: bool) -> None:
	had_changes = pit.invalid()
	saved = pit.save()
	if saved.outcome == PitSaveOutcome.PIT_NOT_LOADED or (had_changes and saved.outcome == PitSaveOutcome.NOTHING_TO_WRITE):
		raise OSError(f"Could not persist {pit.pit_name}: {saved.outcome.value}.")
	if debug:
		sys.stderr.write(f"[{get_cli_prog_name()}] Save: {saved.outcome.value}, change-file fragments: {saved.fragment_count}\n")


def cmd_seed(args: argparse.Namespace) -> int:
	"""
	Ingests entities from stdin, piped jq output, or a JSON5 file.
	Adds entities cloud-safely with leasing.
	CR043: Supports single root entity, keyed entity map, or entity array.
	CR047: Strict patch mode via --require-existing or --patch.
	"""
	require_existing = bool(getattr(args, "require_existing", False) or getattr(args, "patch", False))
	if args.wwwa:
		if args.pit or require_existing or args.source == "-":
			raise JsonPitError("Bulk seed requires --wwwa --source <directory> and accepts neither a PitName nor --patch.")
		for name in ("Person", "Object", "Place", "Activity"):
			options = vars(args) | {"pit": name, "source": str(InqEntry(args.source) / f"{name}.json5"), "wwwa": False}
			code = cmd_seed(argparse.Namespace(**options))
			if code:
				return code
		return 0
	if not args.pit:
		raise JsonPitError("seed requires a PitName or --wwwa.")
	source_path_str = args.source

	# Read source content
	content = ""
	if source_path_str != "-":
		path = InqEntry(source_path_str)
		if not path.is_file():
			sys.stderr.write(f"[jpit] Source file not found: {path}\n")
			return 1
		content = path.read_text(encoding="utf-8")
		source_name = str(path)
	else:
		content = sys.stdin.read()
		source_name = "<stdin>"

	# CR043 Pre-flight validation strictly before opening target Pit or creating directories
	try:
		items_to_add = parse_and_validate_seed_payload(content, source_name)
	except (ValueError, ProtectedAttributeError) as ex:
		msg = str(ex)
		if "contains a prohibited template marker" in msg:
			sys.stderr.write(f"error: {msg}\n")
		else:
			sys.stderr.write(f"[jpit] Error: {msg}\n")
		return 1

	if require_existing and not items_to_add:
		sys.stderr.write("error: Patch source contained 0 entities.\n")
		return 1

	if not items_to_add:
		print(f"[jpit] 0 source items: 0 written, 0 unchanged (Pit '{args.pit}').")
		return 0

	# CR047: Strict patch mode pre-validation before opening target Pit for writing or creating flags
	if require_existing:
		try:
			with _open_cli_pit(
				args.pit,
				cloud=args.cloud,
				root=args.root,
				read_only=True,
				unflagged=True,
			) as living_state:
				for raw_obj in items_to_add:
					item_id = raw_obj["Id"]
					if not living_state.contains(item_id, with_deleted=False):
						sys.stderr.write(
							f"error: Entity '{item_id}' does not exist in Pit '{living_state.pit_name}'. "
							"Use without --id-exists / --patch to allow creating new entities.\n"
						)
						return 1
		except PitNotFoundError:
			sys.stderr.write(
				f"error: Entity '{items_to_add[0]['Id']}' does not exist in Pit '{args.pit}'. "
				"Use without --id-exists / --patch to allow creating new entities.\n"
			)
			return 1
		except Exception as ex:
			sys.stderr.write(f"[jpit] Error: {ex}\n")
			return 1

	with _open_cli_pit(
		args.pit,
		cloud=args.cloud,
		root=args.root,
		retain_window=getattr(args, "retain_window", False),
	) as pit:
		written = 0
		unchanged: list[str] = []
		for raw_obj in items_to_add:
			item = PitItem(raw_obj)
			if pit.add(item):
				written += 1
			else:
				unchanged.append(item.id)
		_save_mutation(pit, getattr(args, "debug", False))

	# The context exit has now completed persistence and required window cleanup.
	if getattr(args, "debug", False):
		sys.stderr.write(f"[jpit] Unchanged IDs: {json.dumps(unchanged, ensure_ascii=False, separators=(',', ':'))}\n")
	print(f"[jpit] {len(items_to_add)} source items: {written} written, {len(unchanged)} unchanged (Pit '{args.pit}').")
	return 0


def cmd_delete(args: argparse.Namespace) -> int:
	"""Tombstones an entity with audited author note and 100s backdating."""
	with _open_cli_pit(
		args.pit,
		cloud=args.cloud,
		root=args.root,
		retain_window=getattr(args, "retain_window", False),
	) as pit:
		success = pit.delete_item(args.id)
		if not success:
			sys.stderr.write(f"[jpit] Entity '{args.id}' is already deleted or not found.\n")
			return 1
		_save_mutation(pit, getattr(args, "debug", False))

	print(f"[jpit] Tombstoned entity '{args.id}' in Pit '{args.pit}'.")
	return 0


def cmd_delete_prop(args: argparse.Namespace) -> int:
	"""Appends a property tombstone at a dot-delimited property path."""
	if args.property_path.lower() in ("id", "modified", "deleted"):
		sys.stderr.write(
			f"[jpit] Error: Cannot tombstone protected attribute '{args.property_path}'. "
			"Use 'jpit delete-item' to delete an entity.\n"
		)
		return 1

	with _open_cli_pit(
		args.pit,
		cloud=args.cloud,
		root=args.root,
		retain_window=getattr(args, "retain_window", False),
	) as pit:
		item = pit.get(args.id)
		if item is None:
			sys.stderr.write(f"[jpit] Entity '{args.id}' not found.\n")
			return 1
		item.delete_property_path(args.property_path)
		_save_mutation(pit, getattr(args, "debug", False))

	print(f"[jpit] Tombstoned property '{args.property_path}' on entity '{args.id}'.")
	return 0


def cmd_export(args: argparse.Namespace) -> int:
	"""Export projected state using pits-compatible directory and stdout modes."""
	out_dir = getattr(args, "out_dir", None)
	json_mode = bool(args.json)
	if bool(out_dir) == json_mode:
		sys.stderr.write("error: export requires exactly one output mode: --json or --out-dir <dir>.\n")
		return 1
	wwwa = bool(getattr(args, "wwwa", False))
	if wwwa == bool(args.pit):
		sys.stderr.write("error: export accepts either <PitName> or --wwwa.\n")
		return 1
	if args.at and not re.search(r"(?:Z|[+-]\d{2}:\d{2})$", args.at, re.IGNORECASE):
		sys.stderr.write("error: --at requires an offset-explicit ISO-8601 timestamp.\n")
		return 1
	at_time = parse_iso_timestamp(args.at) if args.at else None
	base = resolve_command_pit_root(root=args.root, cloud=args.cloud, target_name="WWWA" if wwwa else args.pit,
		operation="export") if args.root or args.cloud or wwwa else None

	def project(name: str) -> tuple[str, list[dict[str, Any]]]:
		target = base / name if base is not None else name
		pit_dir, pit_name = resolve_pit_target(target, cloud=args.cloud, root=args.root)
		canonical = pit_dir / f"{pit_name}.pit"
		if not canonical.is_file():
			raise PitNotFoundError(f"Export source pit does not exist: {canonical}")
		with Pit(pit_dir, pit_name, read_only=True, unflagged=True) as pit:
			items = []
			for key in pit.keys():
				item = pit.get(key, at=at_time)
				if item is not None: items.append(item.to_dict())
			return pit.pit_name, items

	if wwwa:
		data = {name: project(name)[1] for name in WWWA_PITS}
		lookups = {name: {item["Id"]: item for item in items} for name, items in data.items()}
		sections = {"Who": "Person", "What": "Object", "Where": "Place", "Activity": "Activity"}
		resolved = {}
		for name, items in data.items():
			resolved[name] = []
			for item in items:
				result = copy.deepcopy(item)
				for section, target_name in sections.items():
					wrapper = result.get(section)
					if not isinstance(wrapper, dict): continue
					lookup = lookups[target_name]
					unresolved, promoted = {}, {}
					for key, value in wrapper.items():
						if isinstance(value, str) and value in lookup:
							promoted[key] = copy.deepcopy(lookup[value])
						elif isinstance(value, list) and all(isinstance(v, str) and v in lookup for v in value):
							promoted[key] = [copy.deepcopy(lookup[v]) for v in value]
						else: unresolved[key] = value
					result.pop(section)
					result.update(promoted)
					if unresolved: result[section] = unresolved
				resolved[name].append(result)
		data, export_name = resolved, "wwwa"
	else:
		export_name, data = project(args.pit)
	if at_time is not None:
		data = {"_export": {"at": format_iso_timestamp(at_time), "exported": format_iso_timestamp(utcnow())}, "data": data}
	raw_json = json.dumps(data, indent=2, ensure_ascii=False)
	if out_dir:
		directory = InqEntry(out_dir).expanduser()
		directory.mkdir(parents=True, exist_ok=True)
		destination = directory / f"{export_name}.json"
		destination.write_text(raw_json, encoding="utf-8")
		print(f"[jpit] Exported {export_name} to {destination}")
	else:
		sys.stdout.write(raw_json + "\n")
	return 0


# ANSI escape codes for Option B multi-token highlights
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_GREEN = "\033[32m"
C_BRIGHT_GREEN = "\033[92m"
C_CYAN = "\033[36m"
C_BRIGHT_CYAN = "\033[96m"
C_YELLOW = "\033[33m"
C_BRIGHT_YELLOW = "\033[93m"
C_BLUE = "\033[34m"
C_BRIGHT_BLUE = "\033[94m"
C_MAGENTA = "\033[35m"
C_BRIGHT_MAGENTA = "\033[95m"
C_RED = "\033[31m"
C_BRIGHT_RED = "\033[91m"
C_WHITE = "\033[37m"
C_BRIGHT_WHITE = "\033[97m"

# Deterministic positional badge palette for multi-option listings
# 1st: Cyan, 2nd: Green, 3rd: Blue, 4th: Red, 5th: Magenta, 6th: Violet, etc.
# Note: Yellow is intentionally omitted to reserve it exclusively for directory/folder glyphs.
OPTION_BADGE_COLORS: list[str] = [
	C_BRIGHT_CYAN,       # 1st: Cyan
	C_BRIGHT_GREEN,      # 2nd: Green
	C_BRIGHT_BLUE,       # 3rd: Blue
	C_BRIGHT_RED,        # 4th: Red (matching ICloudDrive)
	C_MAGENTA,           # 5th: Magenta
	C_BRIGHT_MAGENTA,    # 6th: Violet / Bright Magenta
	C_CYAN,              # 7th: Deep Cyan
	C_BLUE,              # 8th: Deep Blue
]


def should_color() -> bool:
	"""Determines if colored terminal output should be emitted."""
	if os.getenv("NO_COLOR"):
		return False
	if os.getenv("TERM") == "dumb":
		return False
	return sys.stdout.isatty()


def color(text: str, code: str, use_color: bool = True) -> str:
	"""Wraps text in ANSI escape sequence if color is enabled."""
	if not use_color:
		return text
	return f"{code}{text}{C_RESET}"


WWWA_PITS: tuple[str, ...] = ("Person", "Object", "Place", "Activity")


def parse_duration(text: str) -> datetime.timedelta:
	"""
	Parses a duration string into a datetime.timedelta.
	Supports .NET TimeSpan formats ('7.00:00:00', '01:00:00', '00:10:00')
	as well as friendly unit suffixes ('7d', '1h', '10m', '30s') and seconds.
	"""
	s = str(text).strip()
	if not s:
		raise ValueError("--older-than requires a positive TimeSpan, for example 01:00:00 or 7.00:00:00.")

	# 1. d.hh:mm:ss or d.hh:mm:ss.ffffff
	m = re.match(r"^(\d+)\.(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d+))?$", s)
	if m:
		days = int(m.group(1))
		hours = int(m.group(2))
		minutes = int(m.group(3))
		seconds = int(m.group(4))
		micros = int(m.group(5)[:6].ljust(6, "0")) if m.group(5) else 0
		td = datetime.timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds, microseconds=micros)
		if td <= datetime.timedelta(0):
			raise ValueError("--older-than requires a positive TimeSpan, for example 01:00:00 or 7.00:00:00.")
		return td

	# 2. hh:mm:ss or hh:mm:ss.ffffff
	m = re.match(r"^(\d{1,2}):(\d{2}):(\d{2})(?:\.(\d+))?$", s)
	if m:
		hours = int(m.group(1))
		minutes = int(m.group(2))
		seconds = int(m.group(3))
		micros = int(m.group(4)[:6].ljust(6, "0")) if m.group(4) else 0
		td = datetime.timedelta(hours=hours, minutes=minutes, seconds=seconds, microseconds=micros)
		if td <= datetime.timedelta(0):
			raise ValueError("--older-than requires a positive TimeSpan, for example 01:00:00 or 7.00:00:00.")
		return td

	# 3. mm:ss
	m = re.match(r"^(\d{1,2}):(\d{2})$", s)
	if m:
		minutes = int(m.group(1))
		seconds = int(m.group(2))
		td = datetime.timedelta(minutes=minutes, seconds=seconds)
		if td <= datetime.timedelta(0):
			raise ValueError("--older-than requires a positive TimeSpan, for example 01:00:00 or 7.00:00:00.")
		return td

	# 4. Units with suffix: 7d, 24h, 10m, 30s
	m = re.match(r"^(\d+(?:\.\d+)?)\s*([dhms])$", s.lower())
	if m:
		val = float(m.group(1))
		unit = m.group(2)
		if unit == "d":
			td = datetime.timedelta(days=val)
		elif unit == "h":
			td = datetime.timedelta(hours=val)
		elif unit == "m":
			td = datetime.timedelta(minutes=val)
		elif unit == "s":
			td = datetime.timedelta(seconds=val)
		else:
			raise ValueError(f"Unknown duration unit: {unit}")
		if td <= datetime.timedelta(0):
			raise ValueError("--older-than requires a positive TimeSpan, for example 01:00:00 or 7.00:00:00.")
		return td

	# 5. Raw numeric integer seconds
	if s.isdigit():
		sec = int(s)
		td = datetime.timedelta(seconds=sec)
		if td <= datetime.timedelta(0):
			raise ValueError("--older-than requires a positive TimeSpan, for example 01:00:00 or 7.00:00:00.")
		return td

	raise ValueError(f"--older-than requires a positive TimeSpan, for example 01:00:00 or 7.00:00:00 (got '{s}').")


def cmd_maintain(args: argparse.Namespace) -> int:
	"""
	Reconciles changes, purges expired process flags, and maintains Pit health.
	Provides 100% parity with C# pits maintain.
	"""
	wwwa = getattr(args, "wwwa", False)
	pit_param = getattr(args, "pit", None)

	if wwwa and pit_param:
		sys.stderr.write("[jpit] Error: maintain accepts either <PitName> or --wwwa, not both.\n")
		return 1
	if not wwwa and not pit_param:
		sys.stderr.write("[jpit] Error: maintain requires exactly one <PitName>, or --wwwa.\n")
		return 1

	apply = getattr(args, "apply", False)
	prune = getattr(args, "prune_process_flags", False)
	older_than_raw = getattr(args, "older_than", None)
	repair = getattr(args, "repair_legacy_extensions", False)
	archive_events = getattr(args, "archive_events", False)
	json_output = getattr(args, "json", False)

	if prune and not apply:
		sys.stderr.write("[jpit] Error: --prune-process-flags requires --apply.\n")
		return 1
	if prune and not older_than_raw:
		sys.stderr.write("[jpit] Error: --prune-process-flags requires --older-than <duration>.\n")
		return 1
	if not prune and older_than_raw is not None:
		sys.stderr.write("[jpit] Error: --older-than applies only with --prune-process-flags.\n")
		return 1
	if repair and not apply:
		sys.stderr.write("[jpit] Error: --repair-legacy-extensions requires --apply.\n")
		return 1

	older_than: datetime.timedelta | None = None
	if older_than_raw is not None:
		try:
			older_than = parse_duration(older_than_raw)
		except ValueError as ex:
			sys.stderr.write(f"[jpit] Error: {ex}\n")
			return 1

	options = PitMaintenanceOptions(
		apply=apply,
		prune_process_flags=prune,
		older_than=older_than,
		repair_legacy_extensions=repair,
		archive_events=archive_events,
	)

	cloud = getattr(args, "cloud", "OneDrive")
	root = getattr(args, "root", None)

	targets: list[tuple[InqEntry, InqEntry, str]] = []

	if wwwa:
		try:
			base_dir = resolve_command_pit_root(root=root, cloud=cloud, target_name="WWWA", operation="maintenance")
		except Exception as ex:
			sys.stderr.write(f"[jpit] Error: {ex}\n")
			return 1

		if not base_dir.is_dir():
			sys.stderr.write(f"[jpit] Error: The maintenance root does not exist: {base_dir}\n")
			return 1

		for name in WWWA_PITS:
			p_dir = base_dir / name
			c_file = p_dir / f"{name}.pit"
			targets.append((p_dir, c_file, name))

		if all(not c_file.is_file() for _, c_file, _ in targets):
			sys.stderr.write(f"[jpit] Error: The maintenance root contains none of the WWWA pits: {base_dir}\n")
			return 1
	else:
		try:
			p_dir, p_name = resolve_pit_target(pit_param, cloud=cloud, root=root)
		except Exception as ex:
			sys.stderr.write(f"[jpit] Error: {ex}\n")
			return 1
		c_file = p_dir / f"{p_name}.pit"
		if not c_file.is_file() and not p_dir.is_dir():
			sys.stderr.write(f"[jpit] Error: The requested pit does not exist: {c_file}\n")
			return 1
		targets.append((p_dir, c_file, p_name))

	results: list[PitMaintenanceResult] = []
	for p_dir, c_file, p_name in targets:
		if not c_file.is_file():
			missing = PitMaintenanceResult(pit_file=str(c_file), applied=apply)
			missing.deferred.append(f"Pit does not exist and was not created: {c_file}")
			results.append(missing)
			continue

		with Pit(
			InqPath.from_directory(p_dir),
			p_name,
			read_only=(not apply),
			unflagged=(not apply),
			subscriber="jpit",
			autoload=False,
		) as pit:
			res = pit.maintain(options)
			pit.read_only = True
			results.append(res)

	if json_output:
		doc = [r.to_dict() for r in results] if wwwa else results[0].to_dict()
		sys.stdout.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
	else:
		use_color = should_color()
		for res in results:
			summary = (
				f"{res.pit_file}: changes {res.change_files_observed} observed/{res.change_files_merged} merged/{res.change_files_removed} removed; "
				f"receipts {res.receipts_created} created/{res.receipts_removed} removed; "
				f"flags {res.process_flags_active} active/{res.process_flags_expired} expired/{res.process_flags_pruned} pruned; "
				f"legacy {res.legacy_artifacts_observed} observed/{res.legacy_artifacts_repaired} repaired; "
				f"events {res.event_files_observed} observed/{res.event_files_archived} archived/{res.event_files_removed} removed"
				+ (f" as {res.event_archive_name}." if res.event_archive_name else ".")
			)
			sys.stdout.write(color(summary, C_CYAN, use_color) + "\n")
			for d in res.deferred:
				sys.stdout.write(f"Deferred: {d}\n")
			for f in res.failures:
				err_msg = color(f"Failed: {f}", C_BOLD + C_RED, use_color)
				sys.stderr.write(f"{err_msg}\n")

	return 0 if all(r.succeeded for r in results) else 1


def cmd_audit(args: argparse.Namespace) -> int:
	"""
	Read-only audit of durable JsonPit recovery events (CR003).
	Reads Events directory content without opening a Pit, creating process flags,
	or acquiring leases. Provides 100% parity with C# pits audit.
	"""
	wwwa = getattr(args, "wwwa", False)
	pit_param = getattr(args, "pit", None)

	if wwwa and pit_param:
		sys.stderr.write("[jpit] Error: audit accepts either <PitName> or --wwwa, not both.\n")
		return 1
	if not wwwa and not pit_param:
		sys.stderr.write("[jpit] Error: audit requires exactly one <PitName>, or --wwwa.\n")
		return 1

	machine_filter = getattr(args, "machine", "all") or "all"
	level_raw = getattr(args, "level", "Trace") or "Trace"
	json_output = getattr(args, "json", False)

	try:
		min_level = LogLevel.from_string(level_raw)
	except ValueError as ex:
		sys.stderr.write(f"[jpit] Error: {ex}\n")
		return 1

	cloud = getattr(args, "cloud", "OneDrive")
	root = getattr(args, "root", None)

	directories: list[InqEntry] = []

	if wwwa:
		try:
			base_dir = resolve_command_pit_root(root=root, cloud=cloud, target_name="WWWA", operation="audit")
		except Exception as ex:
			sys.stderr.write(f"[jpit] Error: {ex}\n")
			return 1

		for name in WWWA_PITS:
			directories.append(base_dir / name)
	else:
		try:
			p_dir, _ = resolve_pit_target(pit_param, cloud=cloud, root=root)
		except Exception as ex:
			sys.stderr.write(f"[jpit] Error: {ex}\n")
			return 1
		directories.append(p_dir)

	reads = [
		(d, PitAudit.inspect(d, machine_filter=machine_filter, min_level=min_level))
		for d in directories
	]

	events: list[PitAuditEvent] = []
	for _, res in reads:
		events.extend(res.events)
	events.sort(key=lambda e: (e.machine, e.utc_time, e.event_id))

	issues: list[str] = []
	for d, res in reads:
		for issue in res.issues:
			issues.append(f"{d}: {issue}")

	if json_output:
		sys.stdout.write(json.dumps([e.content for e in events], indent=2) + "\n")
		for issue in issues:
			sys.stderr.write(f"Audit warning: {issue}\n")
		return 0 if len(issues) == 0 else 1

	if len(events) == 0:
		dirs_str = ", ".join(str(d / "Events") for d in directories)
		sys.stdout.write(f"No matching events under {dirs_str}.\n")
		for issue in issues:
			sys.stderr.write(f"Audit warning: {issue}\n")
		return 0 if len(issues) == 0 else 1

	for e in events:
		iso_time = format_iso_timestamp(e.utc_time)
		sys.stdout.write(f"{e.machine}\t{iso_time}\t{e.level.display_name}\t{e.stage}\t{e.message}\t({e.file_name})\n")

	for issue in issues:
		sys.stderr.write(f"Audit warning: {issue}\n")

	return 0 if len(issues) == 0 else 1


def _help_cloud_providers() -> list[str]:
	try:
		cfg = InqConfig.load()
		return [name for name in cfg.default_cloud_order if name in cfg.clouds]
	except Exception:
		return ["OneDrive", "Dropbox", "GoogleDrive", "ICloudDrive"]


def get_cloud_options_description(use_color: bool = True) -> str:
	return help_style.cloud_options(_help_cloud_providers(), use_color)


def print_banner(use_color: bool = True) -> None:
	for line in help_style.banner(f"{Icons.INFO} {get_cli_prog_name()} CLI (v{__version__}) on {platform.node()}", use_color):
		print(line)


def print_top_help(
	nologo: bool = False,
	use_color: bool = True,
	root: str | None = None,
	cloud: str | None = None,
	wwwa: bool = False,
	pit_name: str | None = None,
) -> None:
	if not nologo:
		print_banner(use_color=use_color)
	prog = get_cli_prog_name()
	commands_spec = [
		("grep", "<pattern> [<target>] [-i] [-e] [-p <prop>] [--at <ts>] [--json] [--jq <expr>]"),
		("history", "<PitName> <ItemId> [--jq <expr>]"),
		("list", "-r <tenant|path> [-c <cloud>] [-a|--all] [-l|--long]"),
		("seed", "(<PitName>|--wwwa) --source <file|-> [-x|--id-exists|--patch] (alias: add)"),
		("delete-item", "<PitName> <ItemId> (alias: del)"),
		("delete-property", "<PitName> <ItemId> <PropertyPath> (alias: del-prop)"),
		("export", "(<PitName>|--wwwa) (--out-dir <dir>|--json) [--at <ISO-8601 timestamp>]"),
		("maintain", "(<PitName>|--wwwa) [--apply] [--archive-events] [--prune-process-flags --older-than <dur>] [--json]"),
		("audit", "(<PitName>|--wwwa) [--machine <all|local|name>] [--level <severity>] [--json]"),
	]
	items = [help_style.Row("Commands:", Icons.INFO,
		"list, seed, export, grep, history, audit, delete-property, delete-item, maintain")]
	items.extend(help_style.Row(f"  {prog} {cmd} {spec}", "", "") for cmd, spec in commands_spec)
	items.extend([
		help_style.Row("  seed/add preserves omitted properties. Use del-prop to remove a property.", "", ""),
		help_style.Row("Options:", "", ""),
		help_style.Row("-h, --help", Icons.HELP, "print out all options"),
		help_style.Row("-v, --version", Icons.INFO, "print version info"),
		help_style.Row("-n, --nologo", Icons.NO_BANNER if nologo else Icons.BANNER, "do not display the banner"),
		help_style.Row("-b, --debug", Icons.INFO, "enable debug output"),
		help_style.Row("-r, --pitroot", Icons.FOLDER, str(root) if root else "root directory containing pits"),
		help_style.Row("-c, --cloud", Icons.FOLDER, "", _help_cloud_providers()),
		help_style.Row("-s, --source", Icons.FILE, "source file for import (JSON or JSON5)"),
		help_style.Row("--json", Icons.NOT_AVAILABLE, "export to stdout (for piping to jq, grep, etc.)"),
		help_style.Row("--at", Icons.INFO, "project export history at an offset-explicit ISO-8601 timestamp"),
		help_style.Row("--wwwa", Icons.SUCCESS if wwwa else Icons.NOT_AVAILABLE, "operate on all 4 pits (Person, Object, Place, Activity)"),
		help_style.Row("-w, --window", Icons.INFO, "keep the activity window until timeout"),
		help_style.Row("-x, --id-exists", Icons.INFO, "seed only: require every ID to exist in living state"),
		help_style.Row("--patch", Icons.INFO, "alias for --id-exists"),
	])
	if root:
		try:
			discovered = _discover_tenant_roots(root, cloud, all_clouds=False)[0][2]
		except Exception:
			discovered = []
		pits_str = ", ".join(f"{Icons.letter_box_outline(name)} {name}" for name in discovered) or "(no pits found)"
	else:
		pits_str = "specify -r <root> to discover pits"
	items.append(help_style.Row(f"{Icons.INFO} PitNames", Icons.FOLDER, pits_str))
	if wwwa or _has_wwwa_pit(root, cloud):
		items.append(help_style.Row("WWWA pits:", "", ""))
		for name in WWWA_PITS:
			status, path = _wwwa_pit_status(name, root, cloud)
			items.append(help_style.Row(f"{Icons.INFO} {name}", status, path))
	if pit_name:
		items.extend([
			help_style.Row("Pit argument:", "", ""),
			help_style.Row("", Icons.INFO, "positional argument: select one pit for the chosen command"),
			help_style.Row("", Icons.INFO, f"e.g. '{prog} seed {pit_name} --source patch.json5 -r <root>' seeds {pit_name} from patch.json5"),
		])

	for line in help_style.rows(items, use_color):
		print(line)


def _wwwa_roots(root: str | None, cloud: str | None) -> list[InqEntry]:
	"""Returns resolved help roots without making a cloud selection implicit."""
	if not root:
		return []
	try:
		return [directory for _, directory, _ in _discover_tenant_roots(root, cloud, all_clouds=False)]
	except Exception:
		return []


def _has_wwwa_pit(root: str | None, cloud: str | None) -> bool:
	return any(
		(directory / name / f"{name}.pit").is_file()
		for directory in _wwwa_roots(root, cloud)
		for name in WWWA_PITS
	)


def _wwwa_pit_status(name: str, root: str | None, cloud: str | None) -> tuple[str, str]:
	"""Matches pits' WWWA help line: status glyph plus its resolved Pit file path."""
	roots = _wwwa_roots(root, cloud)
	if not roots:
		return Icons.NOT_AVAILABLE, f"{name}.pit"

	pit_file = roots[0] / name / f"{name}.pit"
	return (Icons.SUCCESS if pit_file.is_file() else Icons.NOT_AVAILABLE), str(pit_file)


def print_command_help(cmd: str, nologo: bool = False, use_color: bool = True) -> None:
	"""Prints formatted help for an individual subcommand."""
	if not nologo:
		print_banner(use_color=use_color)

	alias_map = {"ls": "list", "add": "seed", "del": "delete-item", "del-prop": "delete-property"}
	canonical_cmd = alias_map.get(cmd, cmd)

	help_data: dict[str, dict[str, Any]] = {
		"grep": {
			"usage": "<pattern> [<target>] [options]",
			"desc": "Ripgrep-style living state semantic search across pits.",
			"args": [
				("pattern", "Text or regex to match"),
				("target", "Pit name, file path, or tenant root (default: current directory)"),
			],
			"opts": [
				("-i, --ignore-case", "Case-insensitive search"),
				("-e, --regex", "Treat pattern as regex"),
				("-p, --property <path>", "Scope search to a specific property path"),
				("--at <timestamp>", "Project state as of ISO-8601 timestamp"),
				("--json", "Output JSON stream for piping to jq"),
				("--jq <expr>", "Convenience pipe through jq filter"),
				("--with-deleted", "Include tombstoned entities"),
			],
		},
		"history": {
			"usage": "<PitName> <ItemId> [options]",
			"desc": "Dump immutable fragment history for an entity.",
			"args": [
				("PitName", "Pit name or file path"),
				("ItemId", "Entity ID"),
			],
			"opts": [
				("--jq <expr>", "Convenience pipe through jq filter"),
			],
		},
		"list": {
			"usage": "-r <tenant|path> [-c <cloud>] [-a|--all] [-l|--long]",
			"desc": "Discover pits beneath the selected tenant or directory.",
			"args": [],
			"opts": [("-a, --all", "Search all configured clouds"), ("-l, --long", "Show size and modification time")],
		},
		"seed": {
			"usage": "(<PitName>|--wwwa) --source <file|-> [--id-exists] [--patch] [options]",
			"desc": "seed/add merges supplied properties. Omitted properties are preserved. Use del-prop to remove a property.",
			"args": [
				("PitName", "Pit name or file path"),
				("source", "Source file or '-' for stdin"),
			],
			"opts": [
				("-s, --source <file>", "Source file for import (JSON or JSON5)"),
				("-x, --id-exists", "seed only: require every ID to exist in living state"),
				("--patch", "Alias for --id-exists"),
			],
			"aliases": ["add"],
		},
		"delete-item": {
			"usage": "<PitName> <ItemId> [options]",
			"desc": "Tombstone an entity.",
			"aliases": ["del"],
			"args": [
				("PitName", "Pit name or file path"),
				("ItemId", "Entity ID to tombstone"),
			],
			"opts": [],
		},
		"delete-property": {
			"usage": "<PitName> <ItemId> <PropertyPath> [options]",
			"desc": "Tombstone a property path.",
			"aliases": ["del-prop"],
			"args": [
				("PitName", "Pit name or file path"),
				("ItemId", "Entity ID"),
				("PropertyPath", "Dot-delimited property path (e.g. What.Chat)"),
			],
			"opts": [],
		},
		"export": {
			"usage": "(<PitName>|--wwwa) (--out-dir <dir>|--json) [--at <ISO-8601 timestamp>] [options]",
			"desc": "Export entities as JSON array.",
			"args": [
				("PitName", "Pit name or file path"),
			],
			"opts": [
				("--out-dir <dir>", "Write <PitName>.json into this directory"),
				("--json", "Write JSON to stdout"),
				("--wwwa", "Export all four WWWA pits with resolved references"),
				("--at <timestamp>", "Project state as of timestamp"),
			],
		},
		"maintain": {
			"usage": "(<PitName> | --wwwa) [--apply] [--prune-process-flags --older-than <dur>] [--repair-legacy-extensions] [--archive-events] [--json] [options]",
			"desc": "Inspect or reconcile changes, receipts, expired process flags, and event archives.",
			"args": [
				("PitName", "Pit name or file path (optional if --wwwa is used)"),
			],
			"opts": [
				("--wwwa", "Maintain all four WWWA pits (Person, Object, Place, Activity)"),
				("--apply", "Apply mutations (default is report-only preview)"),
				("--prune-process-flags", "Prune expired process activity flags (requires --apply and --older-than)"),
				("--older-than <dur>", "Duration threshold for flag pruning (e.g. 01:00:00, 7.00:00:00, 1h)"),
				("--repair-legacy-extensions", "Repair legacy event extensions (requires --apply)"),
				("--archive-events", "Archive loose event files into immutable archives"),
				("--json", "Output maintenance report as JSON"),
			],
		},
		"audit": {
			"usage": "(<PitName> | --wwwa) [--machine <all|local|name>] [--level <severity>] [--json] [options]",
			"desc": "Read-only audit of durable JsonPit recovery events without acquiring leases or flags.",
			"args": [
				("PitName", "Pit name or file path (optional if --wwwa is used)"),
			],
			"opts": [
				("--wwwa", "Audit all four WWWA pits (Person, Object, Place, Activity)"),
				("--machine <name>", "Filter events by originating machine ('all', 'local', or machine name)"),
				("--level <severity>", "Minimum severity level filter (default: Trace)"),
				("--json", "Output audit events as JSON array"),
			],
		},
	}

	info = help_data.get(canonical_cmd)
	if not info:
		print_top_help(nologo=nologo, use_color=use_color)
		return

	prog = get_cli_prog_name()
	usage = info["usage"].replace(" | ", "|")
	print(help_style.syntax(f"Usage: {prog} {canonical_cmd} {usage}", enabled=use_color))
	if canonical_cmd == "list":
		print(help_style.syntax(f"       {prog} ls {usage}", enabled=use_color))
	print(help_style.syntax(info["desc"], enabled=use_color))
	items = []
	if info["args"]:
		items.append(help_style.Row("Arguments:", "", ""))
		items.extend(help_style.Row(arg, Icons.FILE, desc) for arg, desc in info["args"])
	if info["opts"]:
		items.append(help_style.Row("Options:", "", ""))
		items.extend(help_style.Row(opt, Icons.INFO, desc) for opt, desc in info["opts"])
	items.extend([
		help_style.Row("Global options:", "", ""),
		help_style.Row("-c, --cloud <name>", Icons.FOLDER, "", _help_cloud_providers()),
		help_style.Row("-r, --pitroot <tenant>", Icons.FOLDER, "root folder or tenant"),
		help_style.Row("-w, --window", Icons.INFO, "keep the activity window until timeout"),
		help_style.Row("-b, --debug", Icons.INFO, "enable debug output"),
		help_style.Row("-n, --nologo", Icons.BANNER, "do not display the banner"),
		help_style.Row("-h, --help", Icons.HELP, "print command usage"),
	])
	for line in help_style.rows(items, use_color): print(line)
	if info.get("aliases"):
		print(help_style.syntax("Aliases: " + ", ".join(f"'{a}'" for a in info["aliases"]), enabled=use_color))


def make_terminal_hyperlink(text: str, uri: str, use_color: bool = True) -> str:
	"""Creates an OSC 8 terminal hyperlink with visual accent if terminal allows."""
	if not use_color:
		return text
	return f"\033]8;;{uri}\033\\{color(text, C_CYAN, use_color)}\033]8;;\033\\"


def print_retain_window_help(nologo: bool = False, use_color: bool = True) -> None:
	"""Prints in-depth help, rationale, and specification links for --retain-window."""
	if not nologo:
		print_banner(use_color=use_color)

	prog = get_cli_prog_name()
	opt_name = help_style.syntax("-w, --window", enabled=use_color)
	sys.stdout.write(f"{color('Option:', C_BOLD + C_CYAN, use_color)}    {opt_name}\n")
	sys.stdout.write(f"{color('Type:', C_BOLD + C_CYAN, use_color)}      Boolean flag (default: False)\n\n")

	sys.stdout.write(f"{color('Summary:', C_BOLD + C_GREEN, use_color)}\n")
	sys.stdout.write("  Keep the ephemeral process activity window file until natural timeout.\n\n")

	sys.stdout.write(f"{color('Lifecycle & Behavior:', C_BOLD + C_GREEN, use_color)}\n")
	sys.stdout.write(
		f"  By default, every finite {prog} operation registers an ephemeral process activity flag\n"
		"  ({Machine}-{Process}-{PID}.flag) during execution to announce its presence across cloud\n"
		"  drives, and cleanly self-deletes its flag upon process exit (CR024).\n\n"
		f"  When --window is specified, {prog} deliberately preserves the owned activity flag\n"
		"  on disk upon exit, leaving it active until its 60-second lease window expires naturally.\n"
		"  (Retained for backward compatibility; scheduled for retirement in the next major release).\n\n"
	)

	sys.stdout.write(f"{color('Specification & Documentation:', C_BOLD + C_GREEN, use_color)}\n")
	pkg_dir = InqEntry(__file__).resolve().parent
	doc_cr_file = pkg_dir.parent / "doc" / "CR" / "CR024_AIA_to_RAIkeep_Ephemeral_Flag_Self_Cleanup.md"
	if doc_cr_file.is_file():
		abs_uri = f"file://{doc_cr_file.as_posix()}#L45"
		rel_jump = "doc/CR/CR024_AIA_to_RAIkeep_Ephemeral_Flag_Self_Cleanup.md:45"
	else:
		abs_uri = "https://github.com/Burkhardt/umgodi-python/blob/main/doc/CR/CR024_AIA_to_RAIkeep_Ephemeral_Flag_Self_Cleanup.md#3---retain-window-compatibility-exception"
		rel_jump = "doc/CR/CR024_AIA_to_RAIkeep_Ephemeral_Flag_Self_Cleanup.md:45"

	github_uri = "https://github.com/Burkhardt/umgodi-python/blob/main/doc/CR/CR024_AIA_to_RAIkeep_Ephemeral_Flag_Self_Cleanup.md#3---retain-window-compatibility-exception"

	cr_title = "CR024 — Ephemeral Process-Flag Self-Cleanup (§3: window compatibility exception)"
	hyperlink = make_terminal_hyperlink(cr_title, abs_uri, use_color=use_color)
	sys.stdout.write(f"  {Icons.HELP} {hyperlink}\n")
	sys.stdout.write(f"    • Local (IDE jump): {color(rel_jump, C_CYAN, use_color)}\n")
	sys.stdout.write(f"    • File URI:         {color(abs_uri, C_DIM, use_color)}\n")
	sys.stdout.write(f"    • GitHub:           {color(github_uri, C_DIM, use_color)}\n\n")


class JsonPitArgumentParser(argparse.ArgumentParser):
	"""Custom ArgumentParser that formats errors with Icons.ERROR and bold red."""

	def error(self, message: str) -> None:
		use_color = should_color()
		self.print_usage(sys.stderr)
		err_content = color(f"{Icons.ERROR} error: {message}", C_BOLD + C_RED, use_color)
		sys.stderr.write(f"{self.prog}: {err_content}\n")
		sys.exit(2)


def get_cli_prog_name(argv0: str | None = None) -> str:
	"""Determines whether CLI is invoked as 'umgodi' or 'jpit'."""
	if argv0 is None:
		argv0 = sys.argv[0] if sys.argv and sys.argv[0] else "jpit"
	base = InqEntry(argv0).name.lower()
	if base == "umgodi" or base.startswith("umgodi"):
		return "umgodi"
	if "umgodi" in str(argv0).lower() and "jpit" not in base:
		return "umgodi"
	return "jpit"


def build_parser(prog: str | None = None) -> JsonPitArgumentParser:
	if prog is None:
		prog = get_cli_prog_name()

	common_parser = JsonPitArgumentParser(add_help=False)
	common_parser.add_argument(
		"-c", "--cloud", "--cloudprovider",
		default=argparse.SUPPRESS,
		help="Cloud drive (OneDrive, Dropbox, etc.)",
	)
	common_parser.add_argument(
		"-r", "--pitroot", "--root", dest="root",
		default=argparse.SUPPRESS,
		help="Root folder / tenant (e.g. AIA, AfricaStage)",
	)
	common_parser.add_argument(
		"-w", "--window", "--retain-window", dest="retain_window",
		action="store_true",
		default=argparse.SUPPRESS,
		help="Keep the activity window until timeout",
	)
	common_parser.add_argument(
		"-n", "--nologo",
		action="store_true",
		default=False,
		help="Do not display the banner",
	)

	common_parser.add_argument("-b", "--debug", action="store_true", default=argparse.SUPPRESS,
		help="Enable debug output on stderr")

	parser = JsonPitArgumentParser(
		prog=prog,
		description=f"{prog} — Cloud-first distributed replicated storage CLI and Pit-Grep.",
		parents=[common_parser],
	)
	parser.add_argument(
		"-v",
		"--version",
		action="version",
		version=f"%(prog)s v{__version__}",
	)

	subparsers = parser.add_subparsers(dest="command", required=True, parser_class=JsonPitArgumentParser)

	# grep (Pit-Grep)
	p_grep = subparsers.add_parser("grep", parents=[common_parser], help="Ripgrep-style living state search across pits")
	p_grep.add_argument("pattern", help="Text or regex to match")
	p_grep.add_argument("target", nargs="?", default=".", help="Pit name, file path, or tenant root (default: current directory)")
	p_grep.add_argument("-i", "--ignore-case", action="store_true", help="Case-insensitive search")
	p_grep.add_argument("-e", "--regex", action="store_true", help="Treat pattern as regex")
	p_grep.add_argument("-p", "--property", help="Scope search to a specific property path")
	p_grep.add_argument("--at", help="Project state as of timestamp")
	p_grep.add_argument("--json", action="store_true", help="Output JSON stream for piping to jq")
	p_grep.add_argument("--jq", help="Convenience pipe through jq filter")
	p_grep.add_argument("--with-deleted", action="store_true", help="Include tombstoned entities")

	# history
	p_hist = subparsers.add_parser("history", parents=[common_parser], help="Dump immutable fragment history for an entity")
	p_hist.add_argument("pit", help="Pit name")
	p_hist.add_argument("id", help="Entity ID")
	p_hist.add_argument("--jq", help="Convenience pipe through jq filter")

	# list
	p_list = subparsers.add_parser("list", aliases=["ls"], parents=[common_parser], help="List active entities or discover tenant pits")
	p_list.add_argument("-a", "--all", action="store_true", help="Discover the tenant in every DefaultCloudOrder provider")
	p_list.add_argument("-l", "--long", action="store_true", help="Include each pit file's size and last modification time")

	# seed / add
	p_seed = subparsers.add_parser(
		"seed",
		aliases=["add"],
		parents=[common_parser],
		help="Ingest JSON5 entities from file or stdin pipe (alias: add)",
	)
	p_seed.add_argument("pit", nargs="?", help="Pit name")
	p_seed.add_argument("--wwwa", action="store_true", help="Seed all four WWWA pits from a directory")
	p_seed.add_argument("-s", "--source", required=True, help="Source file for import (JSON or JSON5)")
	p_seed.add_argument(
		"-x", "--id-exists", "--require-exist", "--require-existing", dest="require_existing",
		action="store_true",
		default=False,
		help="CR047: Strictly require all incoming entity IDs to exist in the Pit",
	)
	p_seed.add_argument(
		"--patch",
		action="store_true",
		default=False,
		help="Alias for --id-exists",
	)

	# del / delete-item
	p_del = subparsers.add_parser(
		"delete-item", aliases=["del"],
		parents=[common_parser],
		help="Tombstone an entity",
	)
	p_del.add_argument("pit", help="Pit name")
	p_del.add_argument("id", help="Entity ID")

	# del-prop / delete-property
	p_delprop = subparsers.add_parser(
		"delete-property", aliases=["del-prop"],
		parents=[common_parser],
		help="Tombstone a property path",
	)
	p_delprop.add_argument("pit", help="Pit name")
	p_delprop.add_argument("id", help="Entity ID")
	p_delprop.add_argument("property_path", help="Dot-delimited property path")

	# export
	p_export = subparsers.add_parser("export", parents=[common_parser], help="Export entities as JSON array")
	p_export.add_argument("pit", nargs="?", default=None, help="Pit name, or use --wwwa")
	p_export.add_argument("--wwwa", action="store_true", help="Export all four WWWA pits")
	p_export.add_argument("--out-dir", help="Export directory for <PitName>.json")
	p_export.add_argument("--json", action="store_true", help="Write JSON to stdout")
	p_export.add_argument("--at", help="Project state as of timestamp")

	# maintain
	p_maintain = subparsers.add_parser(
		"maintain",
		parents=[common_parser],
		help="Reconcile changes, prune expired flags, and maintain pit health",
	)
	p_maintain.add_argument("pit", nargs="?", default=None, help="Pit name or path (optional if --wwwa is used)")
	p_maintain.add_argument("--wwwa", action="store_true", default=False, help="Maintain all four WWWA pits (Person, Object, Place, Activity)")
	p_maintain.add_argument("--apply", action="store_true", default=False, help="Apply maintenance modifications (default is report-only)")
	p_maintain.add_argument("--prune-process-flags", action="store_true", default=False, help="Prune dead process flags older than duration (requires --apply and --older-than)")
	p_maintain.add_argument("--older-than", help="Age threshold for flag pruning (e.g. 01:00:00, 7.00:00:00, 1h)")
	p_maintain.add_argument("--repair-legacy-extensions", action="store_true", default=False, help="Repair legacy event extensions in Events/ (requires --apply)")
	p_maintain.add_argument("--archive-events", action="store_true", default=False, help="Archive loose event files into immutable archives")
	p_maintain.add_argument("--json", action="store_true", default=False, help="Output maintenance report as JSON")

	# audit
	p_audit = subparsers.add_parser(
		"audit",
		parents=[common_parser],
		help="Read-only audit of durable JsonPit recovery events",
	)
	p_audit.add_argument("pit", nargs="?", default=None, help="Pit name or path (optional if --wwwa is used)")
	p_audit.add_argument("--wwwa", action="store_true", default=False, help="Audit all four WWWA pits (Person, Object, Place, Activity)")
	p_audit.add_argument("--machine", default="all", help="Filter by machine name ('all', 'local', or specific machine)")
	p_audit.add_argument("--level", default="Trace", help="Minimum severity level (Trace, Debug, Information, Warning, Error, Critical)")
	p_audit.add_argument("--json", action="store_true", default=False, help="Output audit events as JSON array")

	return parser


def main(argv: list[str] | None = None) -> int:
	if argv is None:
		argv = sys.argv[1:]
	argv = expand_list_flag_bundles(argv)

	use_color = should_color()
	nologo = "-n" in argv or "--nologo" in argv
	cloud, root = extract_cloud_and_root(argv)
	wwwa = "--wwwa" in argv

	known_subcommands = {"list", "ls", "seed", "add", "export", "grep", "history", "audit", "maintain", "delete-item", "delete-property", "del", "del-prop"}

	# Quick check for top-level help or no arguments
	if len(argv) == 0 or (len(argv) == 1 and argv[0] in ("-h", "--help")):
		print_top_help(nologo=nologo, use_color=use_color, root=root, cloud=cloud, wwwa=wwwa)
		return 0

	# Check for command-specific or option-specific help: e.g. jpit --retain-window -h or jpit grep -h
	is_help = "-h" in argv or "--help" in argv or (len(argv) > 0 and argv[0] == "help")
	if is_help:
		target_cmd = next((a for a in argv if a in known_subcommands), None)
		if "--retain-window" in argv and target_cmd is None:
			print_retain_window_help(nologo=nologo, use_color=use_color)
			return 0
		if target_cmd:
			print_command_help(target_cmd, nologo=nologo, use_color=use_color)
			return 0
		pit_name = None
		skip_value = False
		for token in argv:
			if skip_value: skip_value = False; continue
			if token in ("-r", "--pitroot", "--root", "-c", "--cloud"): skip_value = True; continue
			if not token.startswith("-") and token != "help": pit_name = token; break
		print_top_help(nologo=nologo, use_color=use_color, root=root, cloud=cloud, wwwa=wwwa, pit_name=pit_name)
		return 0

	parser = build_parser()
	args = parser.parse_args(argv)
	if args.command == "del":
		args.command = "delete-item"
	elif args.command == "del-prop":
		args.command = "delete-property"

	if not hasattr(args, "cloud"):
		args.cloud = None
	if not hasattr(args, "root"):
		args.root = None

	dispatch = {
		"grep": cmd_grep,
		"history": cmd_history,
		"list": cmd_list_discovery,
		"ls": cmd_list_discovery,
		"seed": cmd_seed,
		"add": cmd_seed,
		"delete-item": cmd_delete,
		"delete-property": cmd_delete_prop,
		"export": cmd_export,
		"maintain": cmd_maintain,
		"audit": cmd_audit,
	}

	cmd_func = dispatch.get(args.command)
	if not cmd_func:
		print_top_help(nologo=nologo, use_color=use_color, root=root, cloud=cloud)
		return 1

	try:
		mutating_commands = {"seed", "add", "delete-item", "delete-property"}
		if args.command in mutating_commands or (args.command == "maintain" and args.apply):
			_require_explicit_mutation_location(args)
		if getattr(args, "debug", False):
			sys.stderr.write(f"[jpit] command={args.command} cloud={args.cloud!r} root={args.root!r}\n")
		return cmd_func(args)
	except OSError as ex:
		sys.stderr.write(f"error: {ex}\n")
		return 1
	except JsonPitError as ex:
		err_icon = color(Icons.ERROR, C_BOLD + C_RED, use_color)
		err_label = color("Pit error:", C_BOLD + C_RED, use_color)
		sys.stderr.write(f"{err_icon} {err_label} {ex}\n")
		return 1
	except Exception as ex:
		err_icon = color(Icons.ERROR, C_BOLD + C_RED, use_color)
		err_label = color("Unexpected error:", C_BOLD + C_RED, use_color)
		sys.stderr.write(f"{err_icon} {err_label} {ex}\n")
		return 2


if __name__ == "__main__":
	sys.exit(main())
