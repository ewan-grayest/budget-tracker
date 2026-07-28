#!/usr/bin/env python3
"""Container entrypoint: prepare the data directory, then exec the app.

Hosting platforms differ in one way that reliably breaks non-root images: an
attached volume shows up owned by root:root, so the unprivileged user baked
into the image cannot write to it. This script adapts instead of assuming.

- Started as root (``--user 0:0``, which several managed hosts require to fix
  volume ownership): create DATA_DIR, hand it to RUN_UID:RUN_GID, then drop
  privileges and exec. The application never runs as root.
- Started unprivileged (the image default): exec straight through. The app
  performs its own writability check and reports an actionable error.

Written in Python because the runtime is guaranteed present in this image,
unlike gosu or su-exec, which would need an extra package.
"""
import grp
import os
import pwd
import sys


def env_str(name, default):
    """Environment override, treating an empty value as unset."""
    value = os.getenv(name)
    return default if value is None or value.strip() == "" else value.strip()


def env_bool(name, default):
    value = env_str(name, "1" if default else "0").lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    fail(f"{name} must be a boolean (1/0, true/false, yes/no, on/off), got {value!r}")


def fail(message):
    print(f"entrypoint: {message}", file=sys.stderr)
    raise SystemExit(1)


def resolve_uid(value):
    """Accept a numeric id or an account name, so RUN_UID=appuser also works."""
    try:
        return int(value)
    except ValueError:
        try:
            return pwd.getpwnam(value).pw_uid
        except KeyError:
            fail(f"RUN_UID={value!r} is not a numeric uid and no such user exists")


def resolve_gid(value):
    try:
        return int(value)
    except ValueError:
        try:
            return grp.getgrnam(value).gr_gid
        except KeyError:
            fail(f"RUN_GID={value!r} is not a numeric gid and no such group exists")


def prepare_data_dir(path, uid, gid, chown):
    """Make DATA_DIR exist and belong to the account the app will run as."""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        fail(f"cannot create DATA_DIR {path!r}: {exc}")
    if not chown:
        return
    try:
        os.chown(path, uid, gid)
        # Group-writable so a host that assigns a different uid on the next
        # start can still write, as long as the gid is preserved.
        os.chmod(path, 0o2775)
        for entry in os.scandir(path):
            os.chown(entry.path, uid, gid)
            if entry.is_file():
                # A database left behind by an earlier container is 0644; widen
                # it so the group keeps write access across a uid change.
                os.chmod(entry.path, entry.stat().st_mode | 0o060)
    except OSError as exc:
        # Dropping CAP_CHOWN is a legitimate configuration; carry on and let
        # the writability check decide whether this is actually fatal.
        print(f"entrypoint: could not adjust ownership of {path!r}: {exc}", file=sys.stderr)


def drop_privileges(uid, gid):
    try:
        os.setgid(gid)
        os.setgroups([gid])
        os.setuid(uid)
    except OSError as exc:
        fail(f"cannot drop privileges to {uid}:{gid}: {exc}")
    if os.getuid() == 0:
        fail("still running as root after dropping privileges, refusing to start")


def main(argv):
    if not argv:
        fail("no command given; expected the application command as arguments")

    data_dir = env_str("DATA_DIR", "/data")
    uid = resolve_uid(env_str("RUN_UID", "10001"))
    gid = resolve_gid(env_str("RUN_GID", "0"))
    # New files group-writable by default, so the database survives a host that
    # assigns a different uid on the next start.
    os.umask(int(env_str("UMASK", "0002"), 8))

    if os.geteuid() == 0:
        prepare_data_dir(data_dir, uid, gid, chown=env_bool("CHOWN_DATA_DIR", True))
        if env_bool("ALLOW_ROOT", False):
            print("entrypoint: ALLOW_ROOT is set, running the application as root", file=sys.stderr)
        else:
            drop_privileges(uid, gid)
    else:
        # Unprivileged already: creating the directory is all we can do, and it
        # only succeeds when the mount is writable anyway.
        prepare_data_dir(data_dir, uid, gid, chown=False)

    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main(sys.argv[1:])
