#!/usr/bin/env python3
"""Private, bounded MySQL backup prerequisite. No SQL or credentials reach stdout."""
import argparse
import contextlib
import datetime as dt
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

MAX_SECONDS = 300
MAX_RAW_BYTES = 1024 * 1024 * 1024
MAX_GZIP_BYTES = 512 * 1024 * 1024
MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_SCHEMA_BYTES = 16 * 1024 * 1024
IDENTIFIER = re.compile(r"[A-Za-z0-9_$]+\Z")
APP_PATHS = (".github", "pool_service", "service_site", "manage.py", "scripts", "update.sh", "requirements.txt")


PHASES = frozenset(("initial", "lock", "checkout", "native_tool", "settings", "client_options",
                   "private_directory", "schema_before", "metadata_lock", "schema_locked",
                   "options_file", "dump_stream", "schema_after", "gzip_verify", "checkout_after", "publish"))
PHASE = "initial"
REASONS = frozenset(("unspecified", "lock_directory_path", "lock_directory_owner_mode",
                     "lock_file_owner_type", "lock_file_mode", "lock_file_replaced",
                     "checkout_head", "checkout_path", "checkout_dirty", "checkout_untracked",
                     "native_missing", "native_version_size", "native_version_unsupported",
                     "native_help_size", "private_path", "private_parent_type", "private_parent_mode",
                     "private_owner_type", "private_mode", "client_engine", "client_database",
                     "client_credentials", "client_options", "client_mirror", "client_port", "app_path",
                     "database_timeout"))


class Blocked(Exception):
    def __init__(self, reason="unspecified"):
        self.reason = reason if type(reason) is str and reason in REASONS else "unspecified"


class NativeFailure(Blocked):
    def __init__(self, returncode):
        super().__init__()
        self.returncode = returncode


def phase(value):
    global PHASE
    PHASE = value


def safe_failure(error):
    """Closed diagnostic vocabulary; never stringify exceptions or native output."""
    category = "guard_failed" if isinstance(error, Blocked) else "unexpected"
    if isinstance(error, NativeFailure):
        category = "native_failed"
    elif isinstance(error, subprocess.TimeoutExpired):
        category = "subprocess_timeout"
    elif isinstance(error, subprocess.CalledProcessError):
        category = "subprocess_failed"
    elif isinstance(error, ImportError):
        category = "import_failed"
    elif isinstance(error, OSError):
        category = "os_error"
    elif type(error).__name__ in {"OperationalError", "ProgrammingError", "DatabaseError", "InterfaceError"}:
        category = "database_error"
    code = getattr(error, "returncode", None)
    code = code if type(code) is int and -127 <= code <= 255 else 0
    reason = error.reason if isinstance(error, Blocked) else "unspecified"
    # Django chains its driver error; PyMySQL chains the original socket error.
    # Inspect bounded types/numbers only, never exception messages or arguments.
    os_errno = 0
    cause = error
    seen = set()
    for _ in range(8):
        if cause is None or id(cause) in seen:
            break
        seen.add(id(cause))
        if isinstance(cause, OSError):
            number = cause.errno
            if not os_errno and type(number) is int and 1 <= number <= 133:
                os_errno = number
            if category == "database_error" and isinstance(cause, TimeoutError):
                reason = "database_timeout"
        cause = cause.__cause__ if cause.__cause__ is not None else cause.__context__
    database_code = error.args[0] if category == "database_error" and error.args else None
    database_code = database_code if type(database_code) is int and 1000 <= database_code <= 2999 else 0
    return ("phase=" + (PHASE if PHASE in PHASES else "initial") + " error=" + category +
            " native_exit=" + str(code) + " reason=" + reason + " os_errno=" + str(os_errno) +
            " database_code=" + str(database_code))


def require(condition, reason="unspecified"):
    if not condition:
        raise Blocked(reason)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def private_dir(path, create=False):
    """Require every path component real, owner-controlled, with private leaf."""
    require(path.is_absolute() and path.resolve() == path, "private_path")
    for parent in (path, *path.parents):
        if parent.exists():
            info = parent.lstat()
            require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode), "private_parent_type")
            # Shared /tmp is permitted only as an ancestor, never as our leaf.
            require(not (info.st_mode & 0o022) or bool(info.st_mode & stat.S_ISVTX), "private_parent_mode")
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid(), "private_owner_type")
    require(stat.S_IMODE(info.st_mode) == 0o700, "private_mode")


def check_checkout(app, expected):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=app, stderr=subprocess.DEVNULL,
                                       timeout=15, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    require(git("rev-parse", "HEAD").decode().strip() == expected, "checkout_head")
    require(git("rev-parse", "--show-toplevel").decode().strip() == str(app), "checkout_path")
    require(not git("diff", "HEAD", "--", *APP_PATHS), "checkout_dirty")
    # Untracked application code can alter Django imports, so it also blocks.
    require(not git("ls-files", "--others", "--exclude-standard", "--", *APP_PATHS), "checkout_untracked")


@contextlib.contextmanager
def deployment_lock(app):
    temporary = app.parent / "tmp"
    require(temporary.resolve() == temporary, "lock_directory_path")
    info = temporary.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o022,
            "lock_directory_owner_mode")
    path = temporary / "service2-deploy.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1,
                "lock_file_owner_type")
        require(not info.st_mode & 0o022, "lock_file_mode")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require((info.st_dev, info.st_ino) == (path.stat().st_dev, path.stat().st_ino), "lock_file_replaced")
        yield
    finally:
        os.close(fd)


def option_value(value):
    value = str(value or "")
    require(not any(ord(char) < 32 or ord(char) == 127 for char in value))
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def client_options(config):
    require(config.get("ENGINE") == "django.db.backends.mysql", "client_engine")
    require(config.get("NAME") and IDENTIFIER.fullmatch(str(config["NAME"])), "client_database")
    require(config.get("USER") and config.get("PASSWORD"), "client_credentials")
    require(set(config.get("OPTIONS", {})) <= {"init_command"}, "client_options")
    require(not config.get("TEST", {}).get("MIRROR"), "client_mirror")
    lines = ["[client]", "user=" + option_value(config["USER"]),
             "password=" + option_value(config["PASSWORD"]), "default-character-set=utf8mb4"]
    host = str(config.get("HOST") or "localhost")
    # PyMySQL uses TCP for empty/localhost HOST; native clients otherwise use a
    # local Unix socket. Pin transport so dump and Django reach the same server.
    lines.append("protocol=" + ("SOCKET" if host.startswith("/") else "TCP"))
    lines.append(("socket=" if host.startswith("/") else "host=") + option_value(host))
    if config.get("PORT"):
        port = str(config["PORT"])
        require(port.isdigit() and 1 <= int(port) <= 65535, "client_port")
        lines.append("port=" + port)
    return "\n".join(lines) + "\n"


def canonical_schema(sql):
    # Only the closing table-options line contains the mutable row counter.
    lines = sql.strip().removesuffix(";").splitlines()
    require(lines and lines[-1].startswith(") "))
    lines[-1] = re.sub(r"\bAUTO_INCREMENT=[0-9]+ ?", "", lines[-1])
    return "\n".join(lines)


def schema_snapshot(connection):
    """No row data; visible unsupported objects fail closed (account visibility only)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT TABLE_NAME, TABLE_TYPE, ENGINE FROM information_schema.TABLES "
                       "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME")
        rows = cursor.fetchall()
        require(0 < len(rows) <= 2000)
        result = {}
        schema_bytes = 0
        for name, kind, engine in rows:
            require(IDENTIFIER.fullmatch(name) and kind == "BASE TABLE" and engine == "InnoDB")
            cursor.execute("SHOW CREATE TABLE `" + name + "`")
            sql = cursor.fetchone()[1]
            size = len(sql.encode("utf-8"))
            schema_bytes += size
            require(size <= MAX_LINE_BYTES and schema_bytes <= MAX_SCHEMA_BYTES)
            result[name] = canonical_schema(sql)
        for source in ("TRIGGERS", "ROUTINES", "EVENTS"):
            schema_column = {"TRIGGERS": "TRIGGER_SCHEMA", "ROUTINES": "ROUTINE_SCHEMA",
                             "EVENTS": "EVENT_SCHEMA"}[source]
            cursor.execute("SELECT COUNT(*) FROM information_schema." + source +
                           " WHERE " + schema_column + " = DATABASE()")
            require(cursor.fetchone()[0] == 0)
    return result


class DumpCoverage:
    """Check native SQL structure while retaining only bounded schema text."""
    def __init__(self, expected):
        self.expected = expected
        self.seen = {}
        self.pending = b""
        self.creating = None
        self.ddl = []
        self.complete = False
        self.raw_bytes = 0

    def feed(self, chunk):
        self.raw_bytes += len(chunk)
        require(self.raw_bytes <= MAX_RAW_BYTES)
        self.pending += chunk
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            require(len(line) <= MAX_LINE_BYTES)
            self.line(line)
        require(len(self.pending) <= MAX_LINE_BYTES)

    def line(self, line):
        require(not self.complete or not line.strip())
        if self.creating is not None:
            self.ddl.append(line)
            require(sum(map(len, self.ddl)) <= MAX_LINE_BYTES)
            if line.startswith(b") ") and line.endswith(b";"):
                name = self.creating
                require(canonical_schema(b"\n".join(self.ddl).decode("utf-8")) == self.expected[name])
                self.seen[name] = True
                self.creating, self.ddl = None, []
            return
        match = re.fullmatch(rb"CREATE TABLE `([A-Za-z0-9_$]+)` \(", line)
        if match:
            name = match[1].decode("ascii")
            require(name in self.expected and name not in self.seen)
            self.creating, self.ddl = name, [line]
        if re.fullmatch(rb"-- Dump completed on [0-9]{4}-[0-9]{2}-[0-9]{2} [0-9: ]+", line):
            self.complete = True

    def finish(self):
        if self.pending:
            self.line(self.pending)
        require(self.complete and self.creating is None and set(self.seen) == set(self.expected))


def stop_child(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    process.wait(timeout=3)


class CappedWriter:
    def __init__(self, stream):
        self.stream = stream

    def write(self, data):
        require(self.stream.tell() + len(data) <= MAX_GZIP_BYTES)
        return self.stream.write(data)

    def flush(self):
        return self.stream.flush()


def dump_to_gzip(command, destination, expected, deadline):
    coverage = DumpCoverage(expected)
    # Never forward inherited app secrets or MYSQL_PWD/MYSQL_* configuration.
    child_env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                 "MYSQL_TEST_LOGIN_FILE": "/dev/null"}
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               env=child_env, start_new_session=True)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as output:
                with gzip.GzipFile(fileobj=CappedWriter(output), mode="wb", filename="", mtime=0) as compressed:
                    while True:
                        require(time.monotonic() < deadline)
                        if not selector.select(timeout=min(1, max(0, deadline - time.monotonic()))):
                            continue
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            break
                        coverage.feed(chunk)
                        compressed.write(chunk)
                        require(output.tell() <= MAX_GZIP_BYTES)
                output.flush()
                require(output.tell() <= MAX_GZIP_BYTES)
                os.fsync(output.fileno())
        returncode = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if returncode != 0:
            raise NativeFailure(returncode)
        coverage.finish()
        return coverage.raw_bytes
    finally:
        stop_child(process)
        process.stdout.close()


def verify_gzip(path, expected_bytes):
    total = 0
    with gzip.open(path, "rb") as stream:
        while chunk := stream.read(65536):
            total += len(chunk)
            require(total <= MAX_RAW_BYTES)
    require(total == expected_bytes)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def native_options(executable):
    version = subprocess.check_output([executable, "--no-defaults", "--version"],
                                      stderr=subprocess.DEVNULL, timeout=5,
                                      env={"PATH": "/usr/bin:/bin", "MYSQL_TEST_LOGIN_FILE": "/dev/null"})
    require(len(version) <= 4096, "native_version_size")
    # MariaDB has no Oracle GTID_PURGED option. All extra option files are off.
    if b"MariaDB" in version:
        return ["--no-tablespaces"]
    require(b"mysqldump" in version and re.search(rb"\bVer\s+[0-9]+\.", version), "native_version_unsupported")
    help_text = subprocess.check_output([executable, "--no-defaults", "--help"],
                                        stderr=subprocess.DEVNULL, timeout=5,
                                        env={"PATH": "/usr/bin:/bin", "MYSQL_TEST_LOGIN_FILE": "/dev/null"})
    require(len(help_text) <= 1024 * 1024, "native_help_size")
    # Oracle 8.0 lacks this option; MYSQL_TEST_LOGIN_FILE=/dev/null isolates
    # login configuration on every supported client, independent of this flag.
    login_flag = ["--no-login-paths"] if b"--no-login-paths" in help_text else []
    return [*login_flag, "--no-tablespaces", "--set-gtid-purged=OFF"]


def backup(app, expected):
    phase("checkout")
    check_checkout(app, expected)
    phase("native_tool")
    executable = shutil.which("mysqldump") or shutil.which("mariadb-dump")
    require(executable is not None, "native_missing")
    tool_options = native_options(executable)
    phase("settings")
    sys.path.insert(0, str(app))
    os.environ["DJANGO_SETTINGS_MODULE"] = "service_site.settings"
    # Import settings/backend without django.setup(): app startup hooks never run.
    from django.db import connections
    connection = connections["default"]
    config = connection.settings_dict
    phase("client_options")
    options = client_options(config)
    connection.settings_dict["OPTIONS"] = {**config.get("OPTIONS", {}), "connect_timeout": 10,
                                           "read_timeout": 15, "write_timeout": 15}
    destination = app.parent / "tmp" / "service2-crm-backups"
    phase("private_directory")
    private_dir(destination, create=True)
    started = now()
    basename = expected + "-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + os.urandom(8).hex()
    partial = destination / (basename + ".sql.gz.partial")
    final = destination / (basename + ".sql.gz")
    receipt_path = destination / (basename + ".receipt.json")
    receipt_partial = destination / (basename + ".receipt.json.partial")
    published = False
    primary_error = None
    try:
        phase("schema_before")
        before = schema_snapshot(connection)
        # Keep metadata locks on these existing tables for the dump duration.
        # InnoDB DML remains available; ALTER/DROP must wait for our rollback.
        phase("metadata_lock")
        connection.set_autocommit(False)
        with connection.cursor() as cursor:
            for name in before:
                cursor.execute("SELECT 1 FROM `" + name + "` LIMIT 0")
        phase("schema_locked")
        require(schema_snapshot(connection) == before)
        phase("options_file")
        with tempfile.TemporaryDirectory(prefix=".client-", dir=destination) as temp:
            options_path = Path(temp) / "client.cnf"
            fd = os.open(options_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(options)
            command = [executable, "--defaults-file=" + str(options_path), *tool_options, "--single-transaction",
                       "--quick", "--skip-lock-tables", "--skip-add-locks", "--hex-blob",
                       "--comments", "--dump-date", "--create-options", "--skip-triggers",
                       "--skip-routines", "--skip-events", "--default-character-set=utf8mb4",
                       "--", str(config["NAME"])]
            phase("dump_stream")
            raw_bytes = dump_to_gzip(command, partial, before, time.monotonic() + MAX_SECONDS - 30)
        phase("schema_after")
        require(schema_snapshot(connection) == before)
        phase("gzip_verify")
        digest = verify_gzip(partial, raw_bytes)
        phase("checkout_after")
        check_checkout(app, expected)
        phase("publish")
        receipt = {"kind": "crm_pilot_db_backup", "schema_version": 1, "expected_sha": expected,
                   "table_count": len(before), "size_bytes": partial.stat().st_size,
                   "uncompressed_bytes": raw_bytes, "sha256": digest, "started_at": started,
                   "completed_at": now(), "integrity_verified": True, "restore_verified": False,
                   "schema_sha256": hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
                   "backup_file": final.name}
        os.rename(partial, final)
        receipt_fd = os.open(receipt_partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(receipt_fd, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(receipt_partial, receipt_path)
        directory_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        published = True
        return receipt
    except BaseException as error:
        primary_error = error
        raise
    finally:
        # A lost socket can make rollback fail again with InterfaceError(0),
        # masking the original database code/cause. Attempt all cleanup, retain
        # the primary failure, and still fail if cleanup alone fails on success.
        cleanup_error = None
        actions = [connection.rollback, connection.close]
        if not published:
            actions.extend(lambda path=path: path.unlink(missing_ok=True)
                           for path in (partial, final, receipt_partial, receipt_path))
        for action in actions:
            try:
                action()
            except BaseException as error:
                if cleanup_error is None:
                    cleanup_error = error
        if primary_error is None and cleanup_error is not None:
            raise cleanup_error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    try:
        require(re.fullmatch(r"[0-9a-f]{40}", args.expected_sha))
        os.umask(0o077)
        app = Path.cwd()
        require(app.resolve() == app and (app / "manage.py").is_file(), "app_path")
        def interrupted(signum, frame):
            raise Blocked()
        for signum in (signal.SIGALRM, signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, interrupted)
        signal.setitimer(signal.ITIMER_REAL, MAX_SECONDS)
        phase("lock")
        with deployment_lock(app):
            # Settings import, SQL, streaming and verification stay under this lock.
            receipt = backup(app, args.expected_sha)
        signal.setitimer(signal.ITIMER_REAL, 0)
        print("CRM_PILOT_DB_BACKUP_V1 " + json.dumps(receipt, sort_keys=True))
        return 0
    except BaseException as error:
        signal.setitimer(signal.ITIMER_REAL, 0)
        print("CRM_PILOT_DB_BACKUP_BLOCKED " + safe_failure(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
