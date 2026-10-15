"""Interactively generate a new dashboard auth value; never reads deployed secrets."""
from __future__ import annotations

import getpass
import hashlib
import json
import secrets
from urllib.parse import urlsplit


def main() -> None:
    origin = input("Public HTTPS origin, without trailing slash: ").strip()
    parsed = urlsplit(origin)
    try:
        port = parsed.port
    except ValueError:
        raise SystemExit("Use a valid HTTPS origin without a trailing slash.") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or origin.endswith("/")
        or any(character.isspace() for character in origin)
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise SystemExit("Use an HTTPS origin such as https://alpha.example, without a trailing slash.")

    password = getpass.getpass("New dashboard password: ")
    if not password or len(password) > 256:
        raise SystemExit("Password must contain 1 to 256 characters.")
    if password != getpass.getpass("Confirm dashboard password: "):
        raise SystemExit("Passwords do not match.")

    salt = secrets.token_bytes(16)
    print(json.dumps({
        "sessionSecret": secrets.token_hex(32),
        "passwordSalt": salt.hex(),
        "passwordHash": hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000).hex(),
        "origins": [origin],
    }, separators=(",", ":")))


if __name__ == "__main__":
    main()
