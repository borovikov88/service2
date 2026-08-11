from django.db import InterfaceError, OperationalError, close_old_connections


def database_error_code(exc):
    """Return only a non-sensitive driver code/category for connection errors."""
    if not isinstance(exc, (OperationalError, InterfaceError)):
        return None
    source = exc.__cause__ or exc
    for value in getattr(source, "args", ()):
        if isinstance(value, int):
            return str(value)
        if isinstance(value, str) and value.isdigit():
            return value
    return "connection"


def run_external_io(call, *args, **kwargs):
    """Run external I/O without carrying an idle DB connection across it."""
    close_old_connections()
    try:
        return call(*args, **kwargs)
    finally:
        # The server may have expired a connection while the HTTP request was
        # in flight.  Discard it before the next ORM operation.
        close_old_connections()
