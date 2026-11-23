import time

from umgodi.canonical import utcnow
from umgodi.store import Pit


def wait_for_returned_ticket(pit: Pit) -> None:
	ticket = pit.master_flag.ticket()
	if ticket is None or ticket.state != "Returned" or ticket.written is None:
		raise ValueError(f"Expected a returned master ticket in {pit.pit_dir}")
	_, pit_sync, _, buffer = pit._load_timing()
	deadline = ticket.written + pit_sync + buffer
	while (remaining := (deadline - utcnow()).total_seconds()) > 0:
		time.sleep(remaining)
