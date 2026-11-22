"""
Flag files and lease management protocol.
Implements the distributed lease protocol from JsonPit.FlagFile:
- MasterFlagFile (Master.flag lease ticket)
- ProcessFlagFile ({Machine}-{App}-{PID}.flag activity windows)
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import os
from inqaba import InqEntry
import socket
import sys
from typing import ClassVar

from .canonical import (
	format_iso_timestamp,
	utcnow,
)
from .fs import safe_delete_file, safe_read_text, safe_write_in_place
from .item import TimestampedValue

GENERIC_MACHINE_NAMES = {
	"localhost",
	"ubuntu",
	"debian",
	"raspberrypi",
	"default",
	"docker",
	"buildkitsandbox",
	"runner",
	"codespaces",
	"devcontainer",
}

GENERIC_PREFIXES = ("desktop-", "win-", "ip-", "vm-")


def get_machine_name() -> str:
	"""Returns the local hostname without domain suffix."""
	return socket.gethostname().split(".")[0]


def get_process_name() -> str:
	"""Returns the executable name of the current process."""
	if sys.argv and sys.argv[0]:
		stem = InqEntry(sys.argv[0]).stem
		if stem:
			return stem
	return "python"


def validate_machine_name(machine_name: str | None = None) -> bool:
	"""
	Validates that the hostname is sufficiently unique to avoid cloud collisions.
	Logs a warning to sys.stderr if generic.
	"""
	name = (machine_name or get_machine_name()).strip()
	if len(name) < 3:
		sys.stderr.write(
			f"[JsonPit] WARNING: MachineName '{name}' is too short to be unique. "
			"Flag file collisions will occur.\n"
		)
		return False
	lower_name = name.lower()
	if lower_name in GENERIC_MACHINE_NAMES:
		sys.stderr.write(
			f"[JsonPit] WARNING: MachineName '{name}' is a generic default. "
			"Flag file collisions will occur.\n"
		)
		return False
	for prefix in GENERIC_PREFIXES:
		if lower_name.startswith(prefix):
			sys.stderr.write(
				f"[JsonPit] WARNING: MachineName '{name}' looks auto-generated. "
				"Consider setting a unique hostname.\n"
			)
			return False
	return True


def participant_of(owner_identity: str) -> str:
	"""
	Extracts the stable participant identity ('{Machine}-{Subscriber}') from a
	master owner identity. Strips trailing numeric PID segment if present.
	"""
	if not owner_identity:
		return owner_identity
	sep = owner_identity.rfind("-")
	if sep > 0 and sep < len(owner_identity) - 1:
		candidate_pid = owner_identity[sep + 1 :]
		if candidate_pid.isdigit():
			return owner_identity[:sep]
	return owner_identity


def is_exact_process_identity(owner_identity: str) -> bool:
	"""Returns True if the owner identity contains a trailing PID segment."""
	return bool(owner_identity and participant_of(owner_identity) != owner_identity)


def flag_name(subscriber: str | None = None) -> str:
	"""Builds the stable participant flag prefix: '{MachineName}-{subscriber}'."""
	sub = subscriber or get_process_name()
	return f"{get_machine_name()}-{sub}"


def current_flag_name(subscriber: str | None = None) -> str:
	"""Builds the PID-specific activity flag stem: '{MachineName}-{subscriber}-{PID}'."""
	return f"{flag_name(subscriber)}-{os.getpid()}"


def current_process_id() -> str:
	"""Returns full diagnostic identity: '{MachineName}:{ProcessName}:{PID}'."""
	return f"{get_machine_name()}:{get_process_name()}:{os.getpid()}"


@dataclass(frozen=True)
class MasterTicket:
	state: str
	owner: str
	claimed: datetime.datetime
	written: datetime.datetime | None = None
	machine: str = ""

	@classmethod
	def parse(cls, line: str) -> MasterTicket:
		fields = line.strip().split("|")
		if len(fields) == 2:
			fields = ["Held", *fields, "", ""]
		state, owner, claimed, written, machine = fields
		if state not in {"Held", "Returned"} or not owner or (state == "Returned" and not (written and machine)):
			raise ValueError("Invalid master ticket")
		def instant(text: str) -> datetime.datetime:
			value = datetime.datetime.fromisoformat(text)
			if value.utcoffset() is None:
				raise ValueError("Expected an offset-explicit ticket timestamp")
			return value
		return cls(state, owner, instant(claimed), instant(written) if written else None, machine)

	def __str__(self) -> str:
		written = format_iso_timestamp(self.written) if self.written else ""
		return f"{self.state}|{self.owner}|{format_iso_timestamp(self.claimed)}|{written}|{self.machine}"


class MasterFlagFile:
	"""
	Master lease ticket file (Master.flag) in the Pit directory.
	Coordinates single-writer lease rights among distributed participants.
	"""

	TICKET_DURATION: ClassVar[datetime.timedelta] = datetime.timedelta(seconds=120)

	def __init__(self, pit_dir: InqEntry, name: str = "Master") -> None:
		self.pit_dir = pit_dir
		self.name = name
		self.path = pit_dir / f"{name}.flag"

	def read(self) -> TimestampedValue:
		"""Reads the current flag file value and timestamp."""
		content = safe_read_text(self.path)
		if not content:
			return TimestampedValue("", datetime.datetime.min.replace(tzinfo=datetime.timezone.utc))
		first_line = content.splitlines()[0] if content.splitlines() else ""
		ticket = MasterTicket.parse(first_line)
		return TimestampedValue(ticket.owner, ticket.claimed)

	def ticket(self) -> MasterTicket | None:
		if not self.path.is_file():
			return None
		return MasterTicket.parse(self.path.read_text(encoding="utf-8"))

	def write_ticket(self, ticket: MasterTicket) -> None:
		safe_write_in_place(self.path, str(ticket))

	@property
	def originator(self) -> str:
		return self.read().value

	@property
	def time(self) -> datetime.datetime:
		return self.read().time

	@property
	def is_expired(self) -> bool:
		"""True when the ticket is absent, empty, or older than TICKET_DURATION."""
		ticket = self.ticket()
		return ticket is None or ticket.state == "Returned" or utcnow() >= ticket.claimed + self.TICKET_DURATION

	def is_owned_by_me(self) -> bool:
		return not self.is_expired and self.originator == get_machine_name()

	def is_owned_by(self, claimant: str) -> bool:
		return not self.is_expired and self.originator == claimant

	def try_claim(self, originator: str | None = None) -> bool:
		"""
		Attempts to claim or renew the master ticket.
		Succeeds when ticket is expired or claimant already holds the ticket.
		"""
		claimant = originator or get_machine_name()
		if not self.is_expired:
			return False
		self.update(time=utcnow(), originator=claimant)
		return True

	def update(
		self,
		time: datetime.datetime | None = None,
		originator: str | None = None,
	) -> TimestampedValue:
		"""Writes a fresh master ticket with given or current timestamp."""
		claimant = originator or get_machine_name()
		t = time or utcnow()
		tv = TimestampedValue(claimant, t)
		self.write_ticket(MasterTicket("Held", claimant, t))
		return tv

	@classmethod
	def conflict_flags(cls, pit_dir: InqEntry) -> list[InqEntry]:
		"""
		Enumerates provider conflict signals: files matching Master*.flag
		whose stem is longer than 'Master'.
		"""
		if not pit_dir.is_dir():
			return []
		results: list[InqEntry] = []
		for p in pit_dir.glob("Master*.flag"):
			if p.stem != "Master" and len(p.name) > len("Master.flag"):
				results.append(p)
		return results


class ProcessFlagFile(MasterFlagFile):
	"""
	PID-specific process activity window flag file:
	'{MachineName}-{Subscriber}-{PID}.flag'.
	Content: '{MachineName}:{ProcessName}:{PID}|{ISO8601-timestamp}'.
	"""

	def __init__(self, pit_dir: InqEntry, subscriber: str | None = None) -> None:
		self.subscriber = subscriber
		stem = current_flag_name(subscriber)
		super().__init__(pit_dir, name=stem)

	def update(
		self,
		time: datetime.datetime | None = None,
		process: str | None = None,
	) -> TimestampedValue:
		"""Updates this process's activity flag with current timestamp and PID info."""
		ident = process or current_process_id()
		t = time or utcnow()
		tv = TimestampedValue(ident, t)
		safe_write_in_place(self.path, str(tv))
		return tv

	@property
	def is_owned_by_current_process(self) -> bool:
		"""True when the flag file exists and content matches current process ID."""
		if not self.path.is_file():
			return False
		tv = self.read()
		return tv.value == current_process_id()

	def try_release_current_process(self) -> bool:
		"""
		Removes this process's activity flag on clean exit.
		Only removes the flag if owned by this exact OS process.
		Never touches Master.flag.
		"""
		if not self.is_owned_by_current_process:
			return False
		return safe_delete_file(self.path)

	@classmethod
	def is_process_window_active(cls, pit_dir: InqEntry, exact_process_identity: str) -> bool:
		"""
		Checks if '{exact_process_identity}.flag' exists and has a timestamp
		within MasterFlagFile.TICKET_DURATION.
		"""
		flag_file = pit_dir / f"{exact_process_identity}.flag"
		if not flag_file.is_file():
			return False
		content = safe_read_text(flag_file)
		if not content:
			return False
		tv = TimestampedValue(content.splitlines()[0])
		return (utcnow() - tv.time) <= MasterFlagFile.TICKET_DURATION

	@classmethod
	def process_window_time(
		cls,
		pit_dir: InqEntry,
		exact_process_identity: str,
	) -> datetime.datetime | None:
		"""Returns the recorded timestamp of a process flag, or None if missing."""
		flag_file = pit_dir / f"{exact_process_identity}.flag"
		if not flag_file.is_file():
			return None
		content = safe_read_text(flag_file)
		if not content:
			return None
		return TimestampedValue(content.splitlines()[0]).time
