import inlay.datoms


def opened(open_db, classes, path, records=()):
    """A db of `open_db` at `path` in which `classes` can be used, holding `records`.

    The classes are registered and the records added in one transaction, so the records are at transaction 1
    in every db; the datoms version, which is paused, still takes its classes in `open` instead.
    """
    if open_db is inlay.datoms.open:
        db = open_db(classes, path=path)
        db.add(*records)
        return db
    db = open_db(path)
    with db.transaction():
        db.register(*classes)
        db.add(*records)
    return db
