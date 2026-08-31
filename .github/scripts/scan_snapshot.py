"""Fail if a DB snapshot contains anything that looks like a credential (D59).

The snapshot is committed to a public branch, so this is the gate: every text
value in every table is checked against known key shapes. Credentials live in
env vars and are never written to the database, so this should always pass — it
exists to make sure that stays true.

Usage: python scan_snapshot.py path/to/trading.db
"""

from __future__ import annotations

import re
import sqlite3
import sys

# Known secret shapes. Deliberately broad; false positives are cheap to fix,
# a leaked key is not.
PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(PK|AK)[A-Z0-9]{18,}\b"), "Alpaca-style API key"),
    (re.compile(r"\bsk-(proj-)?[A-Za-z0-9_-]{20,}\b"), "OpenAI-style secret key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "PEM private key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key id"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "GitHub personal access token"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "Slack token"),
]

# Substrings that are fine even though they sit near key-ish text.
ALLOW = ("alpaca_mcp_security", "untrusted_tool_output")


def main(path: str) -> int:
    con = sqlite3.connect(path)
    con.text_factory = lambda b: b.decode("utf-8", "replace")
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]

    hits: list[str] = []
    scanned = 0
    for table in tables:
        for row in con.execute(f"SELECT * FROM {table}"):  # noqa: S608 — table from sqlite_master
            for value in row:
                if not isinstance(value, str):
                    continue
                scanned += 1
                if any(a in value for a in ALLOW):
                    continue
                for rx, label in PATTERNS:
                    m = rx.search(value)
                    if m:
                        hits.append(f"{table}: {label}: …{m.group(0)[:6]}… "
                                    f"(in a {len(value)}-char value)")
    con.close()

    if hits:
        print("SECRET SCAN FAILED — snapshot NOT published:")
        for h in hits:
            print(f"  - {h}")
        return 1
    print(f"secret scan clean: {len(tables)} tables, {scanned} text values")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
