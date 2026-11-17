"""Every number the task board runs on (A62), in one place.

The privacy policy states several of them in words, and a test holds the policy to this module, so
a change here is a change to what the policy says.
"""

from __future__ import annotations

#: Rounds a new board reads, and the most new rounds an update reads.
ROUNDS = 10
#: Rounds before the first new one that an update reads for context.
CONTEXT_ROUNDS = 2
#: The rendered conversation the model receives, in characters.
WINDOW_MAX_CHARS = 20_000

#: Done items kept, the most recently done first.
DONE_KEPT = 5
#: Open items a board holds before it stops adding new ones.
OPEN_MAX = 25
#: Items on the wire: every open item and the kept done ones.
ITEMS_MAX = 30
#: Characters of one item's text.
ITEM_TEXT_CHARS = 120
#: New items one answer may add.
NEW_ITEMS_MAX = 10
#: Entries one answer may hold before it is refused whole.
OUTPUT_ENTRIES_MAX = 40

#: Days a board is followed after an app last opened it.
FOLLOW_DAYS = 7
#: Days a board nobody opened is kept before the daily sweep deletes it.
UNVIEWED_DAYS = 30

#: Seconds after a turn ends before a followed board is updated.
TURN_END_DEBOUNCE_SECONDS = 20
#: Seconds between two model calls for the same session.
MIN_INTERVAL_SECONDS = 60
#: Model calls this gateway process makes at once.
CONCURRENT_CALLS = 4
#: Model calls one account may cause in a rolling hour: abuse protection, not an allowance.
HOURLY_CALLS_PER_ACCOUNT = 60
#: Seconds a board waits after the first, second and every later failure in a row.
BACKOFF_SECONDS = (60, 300, 1800)
#: Seconds a policy refusal answers for its account without asking again.
VERDICT_CACHE_SECONDS = 60

#: One history page: events asked for, pages per update, seconds and bytes of the reply.
HISTORY_PAGE_EVENTS = 200
HISTORY_PAGES = 3
HISTORY_PAGE_SECONDS = 5
HISTORY_PAGE_BYTES = 4 * 1024 * 1024

#: The model call.
MODEL_TIMEOUT_SECONDS = 45
MAX_OUTPUT_TOKENS = 1500
TEMPERATURE = 0.1

#: Seconds an opened board's view may wait before it is written to the disk.
VIEW_WRITE_SECONDS = 60

DAY_SECONDS = 24 * 3600
