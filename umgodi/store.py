"""
Aggregate Root and storage engine for JsonPit.
Encapsulates directory ecosystems, context manager transactions, distributed lease
coordination, change-file streaming, receipt-based cleanup, and Mapping protocols.
"""

from __future__ import annotations

import abc
import asyncio
import time
import copy
from dataclasses import dataclass, field
import datetime
import enum
import hashlib
import json
from inqaba import InqEntry, InqPath
import threading
from typing import Any, ClassVar, Iterator, MutableMapping
import weakref
import uuid

from .canonical import (
	canonical_json,
	datetime_to_utc_ticks,
	format_iso_timestamp,
	parse_iso_timestamp,
	utcnow,
)
from .changes import ChangeFile, ReceiptFile
from .config import OsConfig, loads_json5
from .exceptions import (
	ObjectDisposedError,
	PitCorruptError,
	PitInstanceConflictError,
	PitNotFoundError,
	ProtectedAttributeError,
	StrictPatchValidationError,
)
from .flags import (
	MasterFlagFile,
	MasterTicket,
	ProcessFlagFile,
	current_flag_name,
	flag_name,
	get_machine_name,
	is_exact_process_identity,
	participant_of,
)
from .fs import (
	ensure_directory,
	resolve_pit_target,
	safe_delete_file,
	safe_read_text,
	safe_write_in_place,
)
from .history import PitItems
from .item import PitItem, TimestampedValue


class PitSaveOutcome(str, enum.Enum):
	NOTHING_TO_WRITE = "NothingToWrite"
	WROTE = "Wrote"
	PUBLISHED_AS_CHANGES = "PublishedAsChanges"
	PIT_NOT_LOADED = "PitNotLoaded"


@dataclass(frozen=True)
class PitSaveResult:
	outcome: PitSaveOutcome
	fragment_count: int = 0

	@classmethod
	def from_changes(cls, count: int) -> PitSaveResult:
		return cls(PitSaveOutcome.PUBLISHED_AS_CHANGES if count else PitSaveOutcome.NOTHING_TO_WRITE, count)


class MutationTrackingMode(enum.Enum):
	"""In-memory mutation policy. Never persisted in a pit or change file."""
	TrackedChangesOnly = "TrackedChangesOnly"
	TrackedChangesWithFallback = "TrackedChangesWithFallback"

	# Upper-case aliases for Pythonic convention
	TRACKED_CHANGES_ONLY = TrackedChangesOnly
	TRACKED_CHANGES_WITH_FALLBACK = TrackedChangesWithFallback


class PitMeta(abc.ABCMeta):
	"""Metaclass allowing class-level properties on Pit (CR003 / v4.5.0 parity)."""
	_default_mutation_tracking_mode: MutationTrackingMode = (
		MutationTrackingMode.TrackedChangesWithFallback
	)

	@property
	def default_mutation_tracking_mode(cls) -> MutationTrackingMode:
		return cls._default_mutation_tracking_mode

	@default_mutation_tracking_mode.setter
	def default_mutation_tracking_mode(cls, value: MutationTrackingMode | str) -> None:
		if isinstance(value, str):
			value = MutationTrackingMode(value)
		if not isinstance(value, MutationTrackingMode):
			raise ValueError(f"Invalid MutationTrackingMode: {value}")
		cls._default_mutation_tracking_mode = value

	@property
	def DefaultMutationTrackingMode(cls) -> MutationTrackingMode:
		return cls.default_mutation_tracking_mode

	@DefaultMutationTrackingMode.setter
	def DefaultMutationTrackingMode(cls, value: MutationTrackingMode | str) -> None:
		cls.default_mutation_tracking_mode = value


class LiveSlot:
	"""Holds current live PitItem instance and accepted baseline."""
	def __init__(
		self,
		id: str = "",
		current: PitItem | None = None,
		baseline: dict[str, Any] | None = None,
	) -> None:
		self.id: str = id
		self.gate = threading.RLock()
		self.current: PitItem | None = current
		self.baseline: dict[str, Any] = baseline if baseline is not None else {}


@dataclass
class TimeValue:
	"""Point-in-time value wrapper matching C# KeyValuePair<DateTimeOffset, JToken>."""
	key: datetime.datetime
	value: Any

	@property
	def Key(self) -> datetime.datetime:
		return self.key

	@property
	def Value(self) -> Any:
		return self.value


def dict_difference(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
	"""
	Objects contribute leaf deltas; arrays remain atomic JSON values.
	Emits None (tombstone) when a property existed in 'before' and is deleted or set to None in 'after'.
	"""
	changes: dict[str, Any] = {}
	all_keys = set(before.keys()) | set(after.keys())
	for key in sorted(all_keys):
		if key not in after or after[key] is None:
			if key in before and before[key] is not None:
				changes[key] = None
			continue
		if key not in before:
			changes[key] = copy.deepcopy(after[key])
			continue
		old_val = before[key]
		new_val = after[key]
		if old_val == new_val:
			continue
		if isinstance(old_val, dict) and isinstance(new_val, dict):
			nested = dict_difference(old_val, new_val)
			if nested:
				changes[key] = nested
		else:
			changes[key] = copy.deepcopy(new_val)
	return changes


def validate_property_mutation_payload(payload: dict[str, Any]) -> None:
	"""Validates that protected attributes are not modified in sparse deltas."""
	for k in payload:
		if k.lower() in ("id", "modified", "deleted"):
			raise ProtectedAttributeError(
				f"Cannot manually mutate protected attribute '{k}' in sparse mutation."
			)


def has_non_empty_string_id(item: Any) -> bool:
	"""
	CR043: Checks if an item is a dictionary with an exact non-empty string 'Id'.
	Lowercase 'id' or non-string/whitespace values are not accepted.
	"""
	if not isinstance(item, dict):
		return False
	val = item.get("Id")
	return isinstance(val, str) and bool(val.strip())


def parse_and_validate_seed_payload(
	payload: str | bytes | Any,
	source_name: str = "source",
) -> list[dict[str, Any]]:
	"""
	CR043: Parses and validates a seed payload strictly BEFORE opening target Pit.
	Three accepted shapes:
	  1. Single Entity Object with an exact, non-empty string 'Id'
	  2. Keyed Entity Map where all values are entity dictionaries
	  3. Entity Array of dictionaries
	Every individual entity must have an exact, non-empty string 'Id' and pass client payload validation.
	"""
	if isinstance(payload, (str, bytes)):
		text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
		if not text.strip():
			raise ValueError(f"Source '{source_name}' is empty.")
		try:
			root = loads_json5(text)
		except Exception as ex:
			raise ValueError(f"Source '{source_name}' contains invalid JSON: {ex}") from ex
	else:
		root = payload

	shape_diagnostic = (
		f"Source '{source_name}' must be a JSON array of entities, a single entity object "
		f"with a non-empty 'Id', or a keyed map of entity objects."
	)

	items: list[Any]
	if isinstance(root, list):
		items = root
	elif isinstance(root, dict) and has_non_empty_string_id(root):
		items = [root]
	elif isinstance(root, dict) and all(isinstance(v, dict) for v in root.values()):
		items = list(root.values())
	else:
		raise ValueError(shape_diagnostic)

	validated: list[dict[str, Any]] = []
	for item in items:
		if not isinstance(item, dict):
			raise ValueError(
				f"{shape_diagnostic} Arrays and keyed maps may contain only JSON objects."
			)
		if not has_non_empty_string_id(item):
			raise ValueError(
				f"Source '{source_name}' contains an entity without a non-empty string 'Id'."
			)
		val = str(item["Id"])
		if "{" in val or "<" in val:
			raise ValueError(
				f"Entity Id '{val}' contains a prohibited template marker ('{{' or '<'). "
				"Resolve template placeholders before writing to a Pit."
			)
		PitItem.validate_client_payload(item)
		validated.append(item)

	return validated


class JsonPitBase:
	"""
	Common base for pit storage with leasing, identity, and persistence coordination.
	"""

	def __init__(
		self,
		pit_dir: InqEntry,
		pit_name: str,
		subscriber: str | None = None,
		read_only: bool = False,
		backup: bool = False,
		unflagged: bool = False,
		retain_window: bool = False,
	) -> None:
		self.pit_dir = InqPath.from_directory(pit_dir)
		self.pit_name = pit_name
		self.canonical_file = self.pit_dir / f"{pit_name}.pit"
		self.subscriber = subscriber
		self.read_only = read_only
		self.backup = backup
		self.unflagged = unflagged
		self.retain_window = retain_window

		self._canonical_seen = self.canonical_file.is_file()
		self._claim_gate = threading.Lock()
		self._claim_time = None
		self._master_flag = MasterFlagFile(self.pit_dir, "Master")
		self._process_flag: ProcessFlagFile | None = None
		self._locker = threading.RLock()

	@property
	def participant_identity(self) -> str:
		"""Stable participant identity: '{MachineName}-{Subscriber}'."""
		return flag_name(self.subscriber)

	@property
	def exact_process_identity(self) -> str:
		"""Exact PID-specific identity: '{MachineName}-{Subscriber}-{PID}'."""
		return current_flag_name(self.subscriber)

	@property
	def master_flag(self) -> MasterFlagFile:
		return self._master_flag

	@property
	def process_flag(self) -> ProcessFlagFile:
		if self._process_flag is None:
			self._process_flag = ProcessFlagFile(self.pit_dir, self.subscriber)
		return self._process_flag

	def running_on_master(self) -> bool:
		"""Quick check: is this exact process currently recorded as the master owner?"""
		if self.unflagged:
			return True
		return self._verify_claim()

	_bans: ClassVar[dict[str, datetime.datetime]] = {}

	def _load_timing(self):
		target = self.pit_dir.resolve()
		roots = [root.resolve() for root in OsConfig.load().clouds.values()]
		root = next((root for root in sorted(roots, key=lambda p: len(p.parts), reverse=True)
			if root in target.parents or root == target), self.pit_dir.parent.parent)
		path = root / "JsonPit" / "timing.json5"
		config = loads_json5(path.read_text(encoding="utf-8")) if path.is_file() else {}
		if not isinstance(config, dict):
			raise ValueError("Expected a timing configuration object")
		values = [config.get(key, default) for key, default in (
			("TicketDurationSeconds", 120), ("SyncPitDurationSeconds", 60),
			("SyncFlagDurationSeconds", 15), ("BufferSeconds", 5))]
		if any(type(v) not in (int, float) or not 0 <= v <= 2147483 for v in values) or values[0] <= sum(values[1:]):
			raise ValueError("Invalid JsonPit timing configuration")
		return tuple(datetime.timedelta(seconds=v) for v in values)

	def try_acquire_master(self) -> tuple[bool, datetime.datetime | None]:
		if not self._claim_gate.acquire(blocking=False):
			return False, None
		try:
			return self._claim_available_ticket()
		finally:
			self._claim_gate.release()

	def _claim_available_ticket(self):
		if self.read_only:
			return False, None
		if self.unflagged:
			return True, utcnow()
		self._lease, pit, flag, buffer = self._load_timing()
		self.master_flag.TICKET_DURATION = self._lease
		self._pit_window = pit + buffer
		key = str(self.canonical_file)
		try:
			previous = self.master_flag.ticket()
		except (ValueError, OSError):
			return False, None
		now = utcnow()
		if key in self._bans and now < self._bans[key]:
			return False, None
		pit_ready = now
		if previous:
			if previous.state == "Held":
				if now < previous.claimed + self._lease:
					return False, None
				pit_ready = now + self._pit_window
			else:
				if previous.machine.casefold() == get_machine_name().casefold() and now < previous.written + self._pit_window:
					return False, None
				pit_ready = min(previous.written, now) + self._pit_window
		self.master_flag.write_ticket(MasterTicket("Held", self.exact_process_identity, now,
			previous.written if previous else None, previous.machine if previous else ""))
		self._claim_time = now
		self.process_flag.update(time=now)
		return True, max(now + flag + buffer, pit_ready)

	def _verify_claim(self) -> bool:
		if self.unflagged:
			return True
		try:
			ticket = self.master_flag.ticket()
		except (ValueError, OSError):
			return False
		return bool(ticket and ticket.state == "Held" and ticket.owner == self.exact_process_identity
			and ticket.claimed == self._claim_time and utcnow() < ticket.claimed + self._lease)

	def _return_claim(self) -> bool:
		if not self.unflagged and self._verify_claim():
			now = utcnow()
			self.master_flag.write_ticket(MasterTicket("Returned", self.exact_process_identity, self._claim_time, now, get_machine_name()))
			self._bans[str(self.canonical_file)] = now + self._pit_window
			return True
		return self.unflagged

	def try_release_process_window(self) -> bool:
		"""Releases this process's activity flag on disk."""
		if self.unflagged or self._process_flag is None:
			return False
		return self._process_flag.try_release_current_process()


@dataclass
class PitMaintenanceOptions:
	"""Configuration options for Pit maintenance (CR021/CR022)."""
	apply: bool = False
	prune_process_flags: bool = False
	older_than: datetime.timedelta | None = None
	repair_legacy_extensions: bool = False
	archive_events: bool = False


@dataclass
class PitMaintenanceResult:
	"""Results of an inspection or apply maintenance operation."""
	pit_file: str
	applied: bool
	master_flags_observed: int = 0
	conflict_flags_observed: int = 0
	process_flags_active: int = 0
	process_flags_expired: int = 0
	process_flags_pruned: int = 0
	process_flags_malformed: int = 0
	change_files_observed: int = 0
	change_files_invalid: int = 0
	change_files_valid: int = 0
	change_files_merged: int = 0
	change_files_eligible: int = 0
	change_files_removed: int = 0
	receipts_observed: int = 0
	receipts_created: int = 0
	receipts_retained: int = 0
	receipts_removed: int = 0
	receipts_malformed: int = 0
	legacy_artifacts_observed: int = 0
	legacy_artifacts_repaired: int = 0
	event_files_observed: int = 0
	event_files_archived: int = 0
	event_files_removed: int = 0
	event_archive_name: str | None = None
	canonical_persisted: bool = False
	current_master: bool = False
	deferred: list[str] = field(default_factory=list)
	failures: list[str] = field(default_factory=list)

	@property
	def succeeded(self) -> bool:
		return len(self.failures) == 0

	def to_dict(self) -> dict[str, Any]:
		return {
			"PitFile": self.pit_file,
			"Applied": self.applied,
			"Succeeded": self.succeeded,
			"MasterFlagsObserved": self.master_flags_observed,
			"ConflictFlagsObserved": self.conflict_flags_observed,
			"ProcessFlagsActive": self.process_flags_active,
			"ProcessFlagsExpired": self.process_flags_expired,
			"ProcessFlagsPruned": self.process_flags_pruned,
			"ProcessFlagsMalformed": self.process_flags_malformed,
			"ChangeFilesObserved": self.change_files_observed,
			"ChangeFilesInvalid": self.change_files_invalid,
			"ChangeFilesValid": self.change_files_valid,
			"ChangeFilesMerged": self.change_files_merged,
			"ChangeFilesEligible": self.change_files_eligible,
			"ChangeFilesRemoved": self.change_files_removed,
			"ReceiptsObserved": self.receipts_observed,
			"ReceiptsCreated": self.receipts_created,
			"ReceiptsRetained": self.receipts_retained,
			"ReceiptsRemoved": self.receipts_removed,
			"ReceiptsMalformed": self.receipts_malformed,
			"LegacyArtifactsObserved": self.legacy_artifacts_observed,
			"LegacyArtifactsRepaired": self.legacy_artifacts_repaired,
			"EventFilesObserved": self.event_files_observed,
			"EventFilesArchived": self.event_files_archived,
			"EventFilesRemoved": self.event_files_removed,
			"EventArchiveName": self.event_archive_name,
			"CanonicalPersisted": self.canonical_persisted,
			"CurrentMaster": self.current_master,
			"Deferred": self.deferred,
			"Failures": self.failures,
		}


class Pit(JsonPitBase, MutableMapping[str, PitItem], metaclass=PitMeta):
	"""
	Domain Aggregate Root for a Pit directory ecosystem.
	Encapsulates entity lifecycles, dirty state tracking, and cloud transactions.
	"""

	_live_instances: ClassVar[dict[str, weakref.ref[Pit]]] = {}
	_registry_lock: ClassVar[threading.Lock] = threading.Lock()

	def __init__(
		self,
		pit_dir: InqEntry,
		pit_name: str,
		subscriber: str | None = None,
		read_only: bool = False,
		backup: bool = False,
		unflagged: bool = False,
		retain_window: bool = False,
		default_max_count: int = 10,
		autoload: bool = True,
		tracking_mode: MutationTrackingMode | str | None = None,
	) -> None:
		super().__init__(
			pit_dir=pit_dir,
			pit_name=pit_name,
			subscriber=subscriber,
			read_only=read_only,
			backup=backup,
			unflagged=unflagged,
			retain_window=retain_window,
		)
		self.default_max_count = default_max_count
		self.autoload = autoload
		self._save_state_initialized = autoload
		if tracking_mode is not None:
			self._tracking_mode = (
				MutationTrackingMode(tracking_mode)
				if isinstance(tracking_mode, str)
				else tracking_mode
			)
		else:
			self._tracking_mode = self.__class__.default_mutation_tracking_mode

		self._historic_items: dict[str, PitItems] = {}
		self._live_items: dict[str, LiveSlot] = {}
		self._disposed = False
		self._owned_canonical_path: str | None = None

		self._register_path_ownership()

		if self.autoload:
			if self.canonical_file.is_file():
				self.load()
			self._merge_pending(self._historic_items)
			self._publish_all_live_state()

	# --- Live Mutation Tracking Mode & Properties ---

	@property
	def tracking_mode(self) -> MutationTrackingMode:
		"""The immutable policy captured for this instance's lifetime in memory."""
		return self._tracking_mode

	@property
	def TrackingMode(self) -> MutationTrackingMode:
		return self._tracking_mode

	@property
	def historic_items(self) -> dict[str, PitItems]:
		return self._historic_items

	@property
	def HistoricItems(self) -> dict[str, PitItems]:
		return self._historic_items

	def _ensure_live_owner(self, live: PitItem) -> None:
		if self._disposed:
			raise ObjectDisposedError(f"Pit '{self.pit_name}' has been closed/disposed.")
		item_id = getattr(live, "_bound_id", None) or live.id
		slot = self._live_items.get(item_id)
		if slot is None or slot.current is not live:
			raise RuntimeError(f"Item '{item_id}' is not the current live item of this Pit.")
		if live.deleted:
			raise RuntimeError(f"Item '{item_id}' has been deleted.")

	def _flush_live_item(self, slot: LiveSlot) -> bool:
		live = slot.current
		if live is None or live._refreshing:
			return False
		current_dict = live.to_dict()
		changes = dict_difference(slot.baseline, current_dict)
		if not changes:
			return False
		try:
			validate_property_mutation_payload(changes)
			sparse_payload = copy.deepcopy(changes)
			item_id = slot.id or getattr(live, "_bound_id", None) or live.id
			sparse_payload["Id"] = item_id
			frag = PitItem(sparse_payload, invalidate=False)
			return self._append_fragment(slot, frag, refresh_modified=True)
		except Exception:
			if slot.baseline:
				live._reconcile_from(slot.baseline)
			raise

	def flush_live_items(self) -> None:
		"""Flushes silent in-place mutations against baseline for all active live items."""
		if self.tracking_mode != MutationTrackingMode.TrackedChangesWithFallback:
			return
		for slot in list(self._live_items.values()):
			with slot.gate:
				self._flush_live_item(slot)

	def accept_live_changes(self, live: PitItem) -> bool:
		"""Receives immediate notification of property/nested mutations from PitItem/ObservableDict."""
		if self._disposed:
			raise ObjectDisposedError(f"Pit '{self.pit_name}' has been closed/disposed.")
		item_id = getattr(live, "_bound_id", None) or live.id
		with self._locker:
			slot = self._live_items.get(item_id)
			if slot is None:
				slot = LiveSlot(id=item_id, current=live, baseline=copy.deepcopy(live.to_dict()))
				self._live_items[item_id] = slot

		with slot.gate:
			try:
				self._ensure_live_owner(live)
				return self._flush_live_item(slot)
			except Exception:
				if slot.baseline:
					live._reconcile_from(slot.baseline)
				raise

	def _append_fragment(
		self,
		slot: LiveSlot,
		item: PitItem,
		refresh_modified: bool = True,
		adopt_item: PitItem | None = None,
	) -> bool:
		with self._locker:
			current = self._historic_items.get(item.id) or PitItems.create(
				item.id, self.default_max_count
			)
			# CR081: match C# AppendFragment, not projected-state equality.
			# Compare before stamping; dict equality conflates False and 0, so use
			# typed canonical JSON for this cross-engine payload comparison.
			top = current.latest_fragment()
			if top is not None:
				previous, incoming = top.to_dict(), item.to_dict()
				previous.pop("Modified", None)
				incoming.pop("Modified", None)
				if canonical_json(previous) == canonical_json(incoming):
					return False
			if refresh_modified:
				item.invalidate()
			else:
				item.mark_pending()
			updated = current.push(item)
			if updated is current:
				return False
			self._historic_items[item.id] = updated

		projected = updated.project_state(with_deleted=True)
		if projected is None:
			slot.current = None
			return True

		if slot.current is None or (slot.current.deleted and not projected.deleted):
			adopted = adopt_item if adopt_item is not None else projected
			adopted.bind(self)
			slot.id = item.id
			slot.current = adopted
			slot.baseline = copy.deepcopy(projected.to_dict())
			adopted._reconcile_from(projected)
		else:
			slot.id = item.id
			slot.baseline = copy.deepcopy(projected.to_dict())
			slot.current._reconcile_from(projected)
		return True

	def _publish_live_state(
		self,
		slot: LiveSlot,
		item_id: str,
		history: PitItems | None,
		original: PitItem | None = None,
	) -> None:
		projected = history.project_state(with_deleted=True) if history else None
		if projected is None:
			slot.current = None
			return
		if slot.current is None or (slot.current.deleted and not projected.deleted):
			adopted = original if original is not None else projected
			adopted.bind(self)
			slot.id = item_id
			slot.current = adopted
			slot.baseline = copy.deepcopy(projected.to_dict())
			adopted._reconcile_from(projected)
		else:
			slot.id = item_id
			slot.baseline = copy.deepcopy(projected.to_dict())
			slot.current._reconcile_from(projected)

	def _publish_all_live_state(self) -> None:
		all_keys = set(self._historic_items.keys()) | set(self._live_items.keys())
		for item_id in all_keys:
			slot = self._live_items.get(item_id)
			if slot is None:
				slot = LiveSlot()
				self._live_items[item_id] = slot
			with slot.gate:
				self._publish_live_state(slot, item_id, self._historic_items.get(item_id))

	# --- InqEntry Ownership Registry (CR003 §4) ---

	def _register_path_ownership(self) -> None:
		canonical_key = str(self.canonical_file.resolve())
		with self._registry_lock:
			if canonical_key in self._live_instances:
				existing_ref = self._live_instances[canonical_key]
				existing = existing_ref()
				if existing is not None and not existing._disposed:
					raise PitInstanceConflictError(
						f"A live Pit instance is already active for '{canonical_key}'."
					)
			self._live_instances[canonical_key] = weakref.ref(self)
			self._owned_canonical_path = canonical_key

	def _release_path_ownership(self) -> None:
		if self._owned_canonical_path is None:
			return
		with self._registry_lock:
			existing_ref = self._live_instances.get(self._owned_canonical_path)
			if existing_ref is not None:
				existing = existing_ref()
				if existing is None or existing is self:
					self._live_instances.pop(self._owned_canonical_path, None)
		self._owned_canonical_path = None

	# --- Context Manager Protocol ---

	def __enter__(self) -> Pit:
		return self

	def __exit__(
		self,
		exc_type: type[BaseException] | None,
		exc_val: BaseException | None,
		exc_tb: Any,
	) -> None:
		try:
			if exc_type is None and not self.read_only:
				if self.has_dirty_items():
					self.save()
		finally:
			self.close(save_dirty=(exc_type is None))

	def close(self, save_dirty: bool = True) -> None:
		"""Explicitly releases process activity flags and path ownership."""
		if self._disposed:
			return
		try:
			self.flush_live_items()
			if save_dirty and not self.read_only:
				if self.has_dirty_items():
					self.save()
			if not self.retain_window:
				released = self.try_release_process_window()
				if not released and self._process_flag is not None and self._process_flag.is_owned_by_current_process:
					raise OSError(f"Could not release the process activity window for Pit '{self.pit_name}'.")
		finally:
			self._release_path_ownership()
			self._disposed = True

	def dispose(self) -> None:
		self.close(save_dirty=True)

	Dispose = dispose

	# --- Factory ---

	@classmethod
	def open(
		cls,
		name_or_path: str | InqEntry,
		cloud: str | None = "OneDrive",
		root: str | None = None,
		subscriber: str | None = None,
		read_only: bool = False,
		backup: bool = False,
		unflagged: bool = False,
		retain_window: bool = False,
		default_max_count: int = 10,
		autoload: bool = True,
		tracking_mode: MutationTrackingMode | str | None = None,
	) -> Pit:
		"""
		Opens a Pit directory using the Cloud-First Triad or explicit local path.

		Examples:
		    with Pit.open("Person", cloud="OneDrive", root="AfricaStage") as pit:
		        ...
		    with Pit.open("/path/to/my/Person") as pit:
		        ...
		"""
		pit_dir, pit_name = resolve_pit_target(name_or_path, cloud=cloud, root=root)
		return cls(
			pit_dir=pit_dir,
			pit_name=pit_name,
			subscriber=subscriber,
			read_only=read_only,
			backup=backup,
			unflagged=unflagged,
			retain_window=retain_window,
			default_max_count=default_max_count,
			autoload=autoload,
			tracking_mode=tracking_mode,
		)

	# --- State Loading & Ingestion ---

	def load(self, undercover: bool = False) -> bool:
		"""
		Loads the canonical .pit snapshot into memory.
		Snapshot format: array of history arrays: [[{fragment}, ...], [{fragment}], ...].
		"""
		if not self.canonical_file.is_file():
			return False

		content = safe_read_text(self.canonical_file)
		if not content or not content.strip():
			return False

		try:
			candidate = self._parse_canonical_snapshot(content)

			with self._locker:
				self.flush_live_items()
				# Preserve unpersisted dirty fragments across replacement of on-disk snapshot
				for k, pit_items in self._historic_items.items():
					for frag in pit_items.history:
						if not frag.is_valid():
							cand_history = candidate.get(k) or PitItems.create(k, self.default_max_count)
							candidate[k] = cand_history.push(frag)

				self._historic_items = candidate
				self._save_state_initialized = True
				self._canonical_seen = True
				self._publish_all_live_state()

			return True
		except Exception as ex:
			if isinstance(ex, PitCorruptError):
				raise
			raise PitCorruptError(f"Failed to parse pit file {self.canonical_file}: {ex}") from ex

	def _parse_canonical_snapshot(self, content: str) -> dict[str, PitItems]:
		data = json.loads(content)
		if not isinstance(data, list):
			raise PitCorruptError(f"Pit snapshot in {self.canonical_file} must be a JSON array.")

		candidate: dict[str, PitItems] = {}
		for entry in data:
			rows = entry if isinstance(entry, list) else [entry]
			for raw in rows:
				if not isinstance(raw, dict) or not raw.get("Id"):
					raise PitCorruptError(f"Invalid canonical history in {self.canonical_file}")
				item = PitItem(raw, invalidate=False)
				history = candidate.get(item.id) or PitItems.create(item.id, self.default_max_count)
				candidate[item.id] = history.push(item)

		return candidate

	def seed_from_file(self, file_path: InqEntry | str, require_existing: bool = False) -> int:
		"""
		Ingests a seed file (JSON or JSON5) and adds all entities to the pit.
		Returns count of imported entities.
		CR043: Validates payload shape and entities strictly before mutation.
		CR047: If require_existing is True, validates that all IDs exist in living state.
		"""
		path = InqEntry(file_path)
		if not path.is_file():
			raise PitNotFoundError(f"Seed file not found: {path}")

		text = path.read_text(encoding="utf-8")
		items_to_add = parse_and_validate_seed_payload(text, str(path))

		if require_existing:
			if not items_to_add:
				raise StrictPatchValidationError("Patch source contained 0 entities.")
			for raw_obj in items_to_add:
				item_id = raw_obj["Id"]
				if not self.contains(item_id, with_deleted=False):
					raise StrictPatchValidationError(
						f"Entity '{item_id}' does not exist in Pit '{self.pit_name}'. "
						"Use without --require-existing / --patch to allow creating new entities."
					)

		count = 0
		for raw_obj in items_to_add:
			item = PitItem(raw_obj)
			self.add(item)
			count += 1
		return count

	# --- Entity Addition & Mutation ---

	def add(self, item: PitItem | dict[str, Any], refresh_modified: bool = True) -> bool:
		"""
		Adds a PitItem as a new historical fragment.
		If refresh_modified is True, refreshes Modified to UtcNow and adopts live item.
		"""
		if not isinstance(item, PitItem):
			if isinstance(item, dict):
				item = PitItem(item)
			else:
				raise TypeError(f"Expected PitItem or dict, got {type(item).__name__}")

		if not item.id or not item.id.strip():
			raise ValueError("PitItem must have a non-empty Id.")

		if item.pit is not None and item.pit is not self:
			raise RuntimeError(f"Item '{item.id}' belongs to another Pit instance.")

		if item.pit is self and refresh_modified:
			# CR077: re-adding an owned live handle is a boundary flush, not a
			# full projection write. Match C# AddCore and preserve sparse history.
			with self._locker:
				slot = self._live_items.get(item.id)
			if slot is None:
				raise RuntimeError(f"Item '{item.id}' is no longer live in this Pit.")
			with slot.gate:
				self._ensure_live_owner(item)
				if self.tracking_mode == MutationTrackingMode.TrackedChangesWithFallback:
					return self._flush_live_item(slot)
				return False

		if refresh_modified:
			item.ensure_valid_for_live_add()

		with self._locker:
			slot = self._live_items.get(item.id)
			if slot is None:
				slot = LiveSlot()
				self._live_items[item.id] = slot

		with slot.gate:
			if self.tracking_mode == MutationTrackingMode.TrackedChangesWithFallback:
				self._flush_live_item(slot)
			adopt = refresh_modified and (slot.current is None or slot.current.deleted)
			return self._append_fragment(
				slot,
				item,
				refresh_modified=refresh_modified,
				adopt_item=item if adopt else None,
			)

	def add_historical(self, item: PitItem) -> bool:
		"""Adds a fragment while preserving its original Modified timestamp intact."""
		return self.add(item, refresh_modified=False)

	def delete_item(self, item_id: str, by: str | None = None, backdate_100: bool = False) -> bool:
		"""
		Tombstones an entity across the distributed mesh.
		Stamps the tombstone with the current wall-clock instant of deletion.
		"""
		tombstone = PitItem(id=item_id)
		if tombstone.delete(by=by, backdate_100=backdate_100):
			res = self.add(tombstone, refresh_modified=True)
			# _append_fragment reconciles the tombstone into the live item under
			# the engine's refresh guard. Do not reassign its protected property.
			return res
		return False

	# --- Entity Addition & Mutation Helpers ---

	def rename_id(self, old_key: str, new_key: str, by: str | None = None) -> bool:
		"""
		Migrates state from old_key to new_key, tombstoning old_key.
		100% parity with C# Pit.RenameId.
		"""
		if not old_key or not new_key or old_key == new_key:
			return False
		if self.get(old_key) is None or self.get(new_key, with_deleted=True) is not None:
			return False

		old_item = self.get(old_key)
		if old_item is None:
			return False

		new_data = old_item.to_dict()
		new_data["Id"] = new_key
		new_data.pop("Modified", None)
		new_data.pop("Deleted", None)
		new_item = PitItem(new_data)

		with self._locker:
			deleted = self.delete_item(old_key, by=by)
			added = self.add(new_item)
			return deleted and added

	def has_dirty_items(self) -> bool:
		"""True if any entity fragment in memory has unpersisted modifications."""
		if self.tracking_mode == MutationTrackingMode.TrackedChangesWithFallback:
			for slot in self._live_items.values():
				if slot.current is not None and not slot.current._refreshing:
					if dict_difference(slot.baseline, slot.current.to_dict()):
						return True
		for pit_items in self._historic_items.values():
			for frag in pit_items._history:
				if not frag.is_valid():
					return True
		return False

	def invalid(self) -> bool:
		return self.has_dirty_items()

	Invalid = invalid

	# --- Persistence & Change Files ---

	def save(self, force: bool = False, pretty: bool = False) -> PitSaveResult:
		"""Report the local canonical write or change-file publication for this save."""
		completed, ready, claim = self._begin_save(force)
		if completed is not None:
			return completed
		return self._store_turn(ready, claim, pretty=pretty)

	Save = save

	def _begin_save(self, force):
		if self.read_only:
			raise OSError(f"Cannot save read-only Pit '{self.pit_name}'.")
		with self._locker:
			if self._disposed or (not self._save_state_initialized and not self._historic_items):
				return PitSaveResult(PitSaveOutcome.PIT_NOT_LOADED), None, None
			self.flush_live_items()
			if not force and not self.has_dirty_items() and not any(self.pit_dir.glob("*.json")):
				return PitSaveResult(PitSaveOutcome.NOTHING_TO_WRITE), None, None
			acquired, ready = self.try_acquire_master()
			if not acquired:
				return PitSaveResult.from_changes(self._create_change_files()), None, None
			return None, ready, self._claim_time

	async def save_async(self, force: bool = False, pretty: bool = False) -> PitSaveResult:
		completed, ready, claim = self._begin_save(force)
		if completed is not None:
			return completed
		await asyncio.sleep(max(0, (ready - utcnow()).total_seconds()))
		return await asyncio.to_thread(self._store_turn, ready, claim, pretty)

	def _store_turn(self, ready, claim, pretty=False, result=None):
		delay = (ready - utcnow()).total_seconds()
		if delay > 0:
			time.sleep(delay)
		with self._locker:
			if self._disposed:
				return PitSaveResult(PitSaveOutcome.PIT_NOT_LOADED)
			if claim != self._claim_time:
				return PitSaveResult.from_changes(self._create_change_files())
			try:
				return self._store_verified(pretty, result)
			except (OSError, ValueError, PitCorruptError):
				if not self.unflagged:
					self._create_change_files()
				raise

	def _check_before_write(self) -> PitSaveResult | None:
		if self.unflagged:
			return None
		losing = []
		unreadable = False
		for path in MasterFlagFile.conflict_flags(self.pit_dir):
			try:
				if MasterTicket.parse(path.read_text(encoding="utf-8")).owner == self.exact_process_identity:
					losing.append(path)
			except (ValueError, OSError):
				unreadable = True
		if not losing and not unreadable and self._verify_claim():
			return None
		count = self._create_change_files()
		now = utcnow()
		event_id = str(uuid.uuid4())
		event = {
			"SchemaVersion": "1", "EventId": event_id, "UtcTime": format_iso_timestamp(now),
			"UtcTicks": datetime_to_utc_ticks(now), "Level": "Warning", "Stage": "ConflictDetected",
			"Pit": str(self.canonical_file), "Machine": get_machine_name(), "Process": self.exact_process_identity,
			"Role": "Loser", "CorrelationId": event_id, "Operation": "Store", "FragmentCount": count,
			"FileCount": len(losing), "Message": "Ticket changed, expired, or conflicted before canonical persistence.",
		}
		path = self.pit_dir / "Events" / f"{event['UtcTicks']}_{self.exact_process_identity}_ConflictDetected.event"
		safe_write_in_place(path, canonical_json(event))
		for path in losing:
			if path.exists() and MasterTicket.parse(path.read_text(encoding="utf-8")).owner == self.exact_process_identity:
				safe_delete_file(path)
		return PitSaveResult.from_changes(count)

	def _store_verified(self, pretty, result):
		result = result or PitMaintenanceResult(str(self.canonical_file), applied=True)
		fallback = self._check_before_write()
		if fallback is not None:
			return fallback
		snapshot = self._read_cleanup_snapshot()
		self.flush_live_items()
		dirty = [f for h in self._historic_items.values() for f in h.history if not f.is_valid()]
		entries = self._merge_pending(snapshot, result)
		for item in dirty:
			snapshot[item.id] = snapshot.get(item.id, PitItems.create(item.id, self.default_max_count)).push(item)
		self._historic_items = snapshot
		self._publish_all_live_state()
		written = self._store_canonical(verify=self._check_before_write, pretty=pretty)
		if written.outcome != PitSaveOutcome.WROTE:
			return written
		if self.canonical_file.stat().st_mtime_ns != self._canonical_write_ns:
			snapshot = self._read_cleanup_snapshot()
		self._receipt_changes(entries, result, snapshot)
		for path in self.pit_dir.glob("*.receipt"):
			change = path.with_suffix(".json")
			if not change.exists():
				receipt = ReceiptFile(change)
				if receipt.is_eligible_for_cleanup and self._verify_claim() and receipt.remove():
					result.receipts_removed += 1
		if not self._return_claim():
			for item in dirty:
				ChangeFile.create(self.pit_dir, item, self.exact_process_identity)
			return PitSaveResult(PitSaveOutcome.WROTE, len(dirty))
		return written

	def _store_canonical(self, verify, pretty: bool = False) -> PitSaveResult:
		dirty_frags: list[PitItem] = []
		model: list[list[dict[str, Any]]] = []

		for key in sorted(self._historic_items.keys()):
			pit_items = self._historic_items[key]
			history_list: list[dict[str, Any]] = []
			for frag in pit_items._history:
				history_list.append(frag.to_dict())
				if not frag.is_valid():
					dirty_frags.append(frag)
			model.append(history_list)


		# Serializes to strict canonical JSON
		if pretty:
			raw_json = json.dumps(model, indent="\t", sort_keys=True, ensure_ascii=False)
		else:
			raw_json = canonical_json(model)

		# CR022 in-place sibling write
		fallback = verify()
		if fallback is not None:
			return fallback
		safe_write_in_place(self.canonical_file, raw_json)
		self._canonical_write_ns = self.canonical_file.stat().st_mtime_ns
		self._canonical_seen = True
		self._save_state_initialized = True

		# Mark all included fragments as clean
		for frag in dirty_frags:
			frag.validate()

		return PitSaveResult(PitSaveOutcome.WROTE)

	def _create_change_files(self) -> int:
		"""Writes all dirty fragments as ordinary collision-safe change files."""
		count = 0
		for pit_items in self._historic_items.values():
			for frag in pit_items._history:
				if not frag.is_valid():
					ChangeFile.create(self.pit_dir, frag, self.exact_process_identity)
					frag.validate()
					count += 1
		return count

	# --- Change Merging & Receipt Cleanup (CR021) ---

	def merge_into_history(self, change_items: PitItems) -> bool:
		"""
		Merges an external history stack into this Pit and reconciles live state.
		100% C# Pit.MergeIntoHistory parity.
		"""
		with self._locker:
			slot = self._live_items.get(change_items.key)
			if slot is None:
				slot = LiveSlot()
				self._live_items[change_items.key] = slot

		with slot.gate:
			if self.tracking_mode == MutationTrackingMode.TrackedChangesWithFallback:
				self._flush_live_item(slot)
			with self._locker:
				current = self._historic_items.get(change_items.key) or PitItems.create(
					change_items.key, self.default_max_count
				)
				updated = current
				for item in change_items.history:
					updated = updated.push(item)
				if updated == current:
					return False
				self._historic_items[change_items.key] = updated
			self._publish_live_state(slot, change_items.key, updated)
			return True

	MergeIntoHistory = merge_into_history

	def _merge_pending(self, snapshot, result=None):
		entries = []
		for path in sorted(self.pit_dir.glob("*.json"), key=lambda p: p.name, reverse=True):
			if result is not None:
				result.change_files_observed += 1
			payload = ChangeFile.read_validated(path)
			try:
				if payload is None:
					raise ValueError("Incomplete or invalid change file")
				fragments = []
				for row in payload:
					for raw in (row if isinstance(row, list) else [row]):
						if not isinstance(raw, dict) or not raw.get("Id"):
							raise ValueError("Expected a fragment with an Id")
						fragments.append(PitItem(raw, invalidate=False))
			except (ValueError, TypeError):
				if result is not None:
					result.change_files_invalid += 1
				continue
			for item in fragments:
				history = snapshot.get(item.id) or PitItems.create(item.id, self.default_max_count)
				snapshot[item.id] = history.push(item)
			entries.append((path, payload))
			if result is not None:
				result.change_files_valid += 1
				result.change_files_merged += 1
		return entries

	def merge_changes(self) -> int:
		"""Merge pending changes and use a write turn when opened for persistence."""
		return self.maintain(apply=not self.read_only).change_files_merged

	# --- Maintenance & Cleanup (CR021/CR022) ---

	def _is_current_exact_master(self) -> bool:
		return self._verify_claim()

	def _receipt_changes(self, merged_files, result, snapshot):
		for cf, payload in merged_files:
			if ChangeFile.try_parse_name(cf.stem) is None:
				continue
			if not self._canonical_accounts_for(snapshot, payload):
				result.deferred.append(f"Canonical pit does not yet account for {cf.name}")
				continue
			try:
				existed = ReceiptFile.path_for(cf).is_file()
				receipt = ReceiptFile(cf)
				result.receipts_observed += int(existed)
				result.receipts_created += int(not existed)
				result.receipts_retained += 1
				if not receipt.is_eligible_for_cleanup:
					continue
				result.change_files_eligible += 1
				if not self.canonical_file.is_file() or not self._is_current_exact_master():
					result.deferred.append(f"Cleanup revalidation deferred {cf.name}")
					continue
				if not safe_delete_file(cf):
					result.failures.append(f"Change file remained after removal: {cf.name}")
					continue
				result.change_files_removed += 1
				if receipt.remove():
					result.receipts_removed += 1
				else:
					result.failures.append(f"Receipt remained after removal: {receipt.path.name}")
			except (OSError, ValueError) as exc:
				result.receipts_malformed += 1
				result.deferred.append(f"Receipt is not currently valid: {cf.name}: {exc}")

	def _read_cleanup_snapshot(self) -> dict[str, PitItems]:
		if not self.canonical_file.is_file():
			if self._canonical_seen:
				raise PitCorruptError(f"Missing previously observed canonical {self.canonical_file}")
			return {}
		content = safe_read_text(self.canonical_file)
		if not content or not content.strip():
			raise PitCorruptError(f"Cannot read canonical file {self.canonical_file}")
		return self._parse_canonical_snapshot(content)

	@staticmethod
	def _canonical_accounts_for(snapshot: dict[str, PitItems], payload: list[Any]) -> bool:
		for entry in payload:
			rows = entry if isinstance(entry, list) else [entry]
			if not rows:
				return False
			for raw in rows:
				if not isinstance(raw, dict) or "Id" not in raw:
					return False
				fragment = PitItem(raw, invalidate=False)
				history = snapshot.get(fragment.id)
				if history is None or history.push(fragment).history != history.history:
					return False
		return bool(payload)

	def maintain(
		self,
		options: PitMaintenanceOptions | None = None,
		*,
		apply: bool = False,
	) -> PitMaintenanceResult:
		"""
		Inspects or reconciles changes, receipts, process flags, and event archives.
		Report-only by default; apply=True performs authorized modifications.
		"""
		if options is None:
			options = PitMaintenanceOptions(apply=apply)
		elif apply:
			options.apply = True

		if options.prune_process_flags and not options.apply:
			raise ValueError("Process-flag pruning requires apply=True.")
		if options.prune_process_flags and options.older_than is None:
			raise ValueError("Process-flag pruning requires older_than duration.")
		if not options.prune_process_flags and options.older_than is not None:
			raise ValueError("older_than applies only to process-flag pruning.")
		if options.older_than is not None and options.older_than <= datetime.timedelta(0):
			raise ValueError("older_than must be positive.")

		result = PitMaintenanceResult(
			pit_file=str(self.canonical_file),
			applied=options.apply,
		)

		if not self.pit_dir.is_dir():
			result.deferred.append(f"Pit directory does not exist: {self.pit_dir}")
			return result

		now = utcnow()

		if not options.apply and not self._historic_items and self.canonical_file.is_file():
			self.load(undercover=True)

		if options.apply and (self.has_dirty_items() or any(self.pit_dir.glob("*.json")) or any(self.pit_dir.glob("*.receipt"))):
			result.current_master, ready = self.try_acquire_master()
			if result.current_master:
				saved = self._store_turn(ready, self._claim_time, result=result)
				result.canonical_persisted = saved.outcome == PitSaveOutcome.WROTE
		with self._locker:
			if not result.current_master:
				self.flush_live_items()
				if options.apply and not self.read_only:
					self._create_change_files()
				entries = self._merge_pending(self._historic_items, result)
				self._publish_all_live_state()
				if options.apply and entries:
					result.deferred.append("Cleanup evidence and deletion require current exact-master authority.")
			if not options.apply:
				for cf, _ in entries:
					rf_path = cf.with_suffix(".receipt")
					if rf_path.is_file():
						result.receipts_observed += 1
						try:
							receipt = ReceiptFile(cf)
							if receipt.is_eligible_for_cleanup:
								result.change_files_eligible += 1
							result.receipts_retained += 1
						except Exception:
							result.receipts_malformed += 1

			# 3. Process Flags inspection and optional pruning
			for pf in sorted(self.pit_dir.glob("*.flag"), key=lambda p: p.name):
				if pf.name == "Master.flag" or pf.name == "Master":
					result.master_flags_observed += 1
					continue
				if pf.name.startswith("Master"):
					result.conflict_flags_observed += 1
					continue

				try:
					content = safe_read_text(pf)
					first_line = content.splitlines()[0] if content else ""
					if not first_line:
						result.process_flags_malformed += 1
						result.deferred.append(f"Process flag is malformed: {pf.name}")
						continue
					tv = TimestampedValue(first_line)
					is_expired = (now - tv.time) > MasterFlagFile.TICKET_DURATION
					if not is_expired:
						result.process_flags_active += 1
						continue

					result.process_flags_expired += 1
					if options.prune_process_flags and options.older_than is not None:
						age = now - tv.time
						if age >= options.older_than:
							reread_content = safe_read_text(pf)
							reread_line = reread_content.splitlines()[0] if reread_content else ""
							if not is_exact_process_identity(pf.stem) or not tv.value.endswith(":" + pf.stem.rsplit("-", 1)[-1]):
								result.deferred.append(f"Process flag filename/content PID mismatch: {pf.name}")
								continue
							if reread_content == content and len(content.splitlines()) == 1 and reread_line:
								safe_delete_file(pf)
								if not pf.exists():
									result.process_flags_pruned += 1
								else:
									result.failures.append(f"Expired process flag remained after removal: {pf}")
				except Exception as ex:
					result.process_flags_malformed += 1
					result.deferred.append(f"Process flag is not currently verifiable: {pf.name}: {ex}")

			# 4. Legacy EventFile migration
			events_dir = self.pit_dir / "Events"
			if events_dir.is_dir():
				for ef in sorted(events_dir.glob("*.event"), key=lambda p: p.name):
					result.event_files_observed += 1
					stem = ef.stem
					if "_" in stem:
						prefix, possible_hash = stem.rsplit("_", 1)
						if len(possible_hash) == 64 and all(c in "0123456789abcdefABCDEF" for c in possible_hash):
							result.legacy_artifacts_observed += 1
							if options.repair_legacy_extensions or options.apply:
								content = safe_read_text(ef)
								actual_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
								if actual_hash.lower() == possible_hash.lower():
									clean_target = events_dir / f"{prefix}.event"
									try:
										ef.rename(clean_target)
										result.legacy_artifacts_repaired += 1
									except Exception as ex:
										result.failures.append(f"Legacy event rename failed for {ef.name}: {ex}")
								else:
									result.deferred.append(f"Legacy event file hash mismatch for {ef.name}")

		return result

	# --- Query & Mapping Protocol ---

	def get(
		self,
		item_id: str,
		default: Any = None,
		*,
		at: datetime.datetime | None = None,
		with_deleted: bool = False,
	) -> PitItem | None:
		"""
		Returns the projected live state of an entity.
		If at is specified, returns an immutable detached historical snapshot.
		If with_deleted is False, returns None for tombstoned entities.
		"""
		if self._disposed:
			raise ObjectDisposedError(f"Pit '{self.pit_name}' has been closed/disposed.")
		if not item_id:
			return default

		if at is not None:
			pit_items = self._historic_items.get(item_id)
			if pit_items is None:
				return default
			projected = pit_items.project_state(at=at, with_deleted=with_deleted)
			if projected is None:
				return default
			return PitItem(projected, invalidate=False)

		with self._locker:
			slot = self._live_items.get(item_id)
			if slot is None:
				slot = LiveSlot()
				self._live_items[item_id] = slot

		with slot.gate:
			if slot.current is not None:
				if slot.current.deleted and not with_deleted:
					return default
				return slot.current

			pit_items = self._historic_items.get(item_id)
			if pit_items is None:
				return default
			self._publish_live_state(slot, item_id, pit_items)
			if slot.current is not None and not (slot.current.deleted and not with_deleted):
				return slot.current
			return default

	def get_at(
		self,
		item_id: str,
		at: datetime.datetime,
		with_deleted: bool = False,
	) -> PitItem | None:
		"""Point-in-time historical projection of an entity."""
		return self.get(item_id, at=at, with_deleted=with_deleted)

	def values_over_time(self, item_id: str, property_name: str) -> list[TimeValue]:
		"""100% C# parity: historical property values across all fragments."""
		pit_items = self._historic_items.get(item_id)
		if pit_items is None:
			return []
		results: list[TimeValue] = []
		for frag in pit_items.history:
			val = None if frag.deleted else frag.get(property_name)
			results.append(TimeValue(frag.modified, copy.deepcopy(val)))
		return results

	def contains(self, item_id: str, with_deleted: bool = False) -> bool:
		"""100% C# parity: checks if an entity exists in living state."""
		if not item_id:
			return False
		return self.get(item_id, with_deleted=with_deleted) is not None

	def all_undeleted(self) -> list[PitItem]:
		"""Returns all active, non-deleted entities in this pit."""
		results: list[PitItem] = []
		for key in sorted(self._historic_items.keys()):
			item = self.get(key)
			if item is not None and not item.deleted:
				results.append(item)
		return results

	def export_json(
		self,
		export_path: InqEntry | str,
		at: datetime.datetime | None = None,
		pretty: bool = True,
	) -> InqEntry:
		"""Exports undeleted entity projections to a JSON file."""
		if at is None:
			self.flush_live_items()
		target = InqEntry(export_path)
		ensure_directory(target.parent)

		items = [
			self.get(key, at=at).to_dict()
			for key in sorted(self._historic_items.keys())
			if self.get(key, at=at) is not None
		]

		if pretty:
			content = json.dumps(items, indent="\t", sort_keys=True, ensure_ascii=False)
		else:
			content = canonical_json(items)

		safe_write_in_place(target, content)
		return target

	# --- C# Compatibility Aliases ---
	Get = get
	GetAt = get_at
	ValuesOverTime = values_over_time
	Contains = contains
	AllUndeleted = all_undeleted
	ExportJson = export_json
	Load = load
	Delete = delete_item
	RenameId = rename_id

	# --- MutableMapping Interface ---

	def __getitem__(self, item_id: str) -> PitItem:
		val = self.get(item_id)
		if val is None:
			raise KeyError(item_id)
		return val

	def __setitem__(self, item_id: str, item: PitItem | dict[str, Any]) -> None:
		if isinstance(item, PitItem):
			if item.id != item_id:
				item = item.clone()
				item.id = item_id
			self.add(item)
		elif isinstance(item, dict):
			payload = copy.deepcopy(item)
			payload["Id"] = item_id
			self.add(PitItem(payload))
		else:
			raise TypeError(f"Cannot assign {type(item).__name__} to Pit.")

	def __delitem__(self, item_id: str) -> None:
		deleted = self.delete_item(item_id)
		if not deleted:
			raise KeyError(item_id)

	def __contains__(self, item_id: object) -> bool:
		if not isinstance(item_id, str):
			return False
		return self.contains(item_id)

	def __iter__(self) -> Iterator[str]:
		for key in sorted(self._historic_items.keys()):
			if self.get(key) is not None:
				yield key

	def __len__(self) -> int:
		return sum(1 for _ in self)

	def __repr__(self) -> str:
		return f"<Pit '{self.pit_name}' path={self.pit_dir} items={len(self)}>"


Pit.parse_and_validate_seed_payload = staticmethod(parse_and_validate_seed_payload)  # type: ignore[attr-defined]
PitStore = Pit
