"""An experiment: the history db storing datoms, one for each field of a record, instead of rows.

It started as a copy of `inlay.history`. See "The datoms version" in notes/2026-11-13-immutable-db.md.
"""

from inlay.datoms.db import Db, RecordSet, RecordView, Snapshot, open

__all__ = ["Db", "RecordSet", "RecordView", "Snapshot", "open"]
