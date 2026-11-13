"""An experiment: inlay with history, so the data can be read as it was at any past transaction.

See notes/2026-11-13-immutable-db.md.
"""

from inlay.history.db import Db, RecordSet, RecordView, Snapshot, open

__all__ = ["Db", "RecordSet", "RecordView", "Snapshot", "open"]
