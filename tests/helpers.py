import inlay.datoms


def opened(open_db, classes, path):
    """A db of `open_db` at `path` in which `classes` can be used.

    The datoms version, which is paused, still takes its classes in `open`; the others register them.
    """
    if open_db is inlay.datoms.open:
        return open_db(classes, path=path)
    db = open_db(path)
    db.register(*classes)
    return db
