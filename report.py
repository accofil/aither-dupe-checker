"""Read-only decision report for the Aither dupe checker (no network)."""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

STATUS_ORDER = (
    "NOT_FOUND",
    "MANUAL",
    "UNRESOLVED",
    "RECHECK_REQUIRED",
    "DUPLICATE",
    "NOT_MEDIA",
    "BLACKLISTED",
)
ATTENTION = {"NOT_FOUND", "MANUAL", "UNRESOLVED", "RECHECK_REQUIRED"}
COLUMNS = ("status", "reason", "name", "tmdb", "imdb", "aither_match", "message", "lint")


def _gib(value: Any) -> str:
    try:
        return f"{int(value) / 1024 ** 3:.2f} GiB"
    except (TypeError, ValueError):
        return "?"


def _match_text(record: Mapping[str, Any]) -> str:
    evidence = record.get("matched_evidence")
    if not isinstance(evidence, Mapping):
        return ""
    attrs = evidence.get("attributes")
    attrs = attrs if isinstance(attrs, Mapping) else evidence
    name = str(attrs.get("name") or "").strip()
    if not name:
        return ""
    link = str(attrs.get("details_link") or "").split("?")[0]
    flags = [
        label
        for key, label in (("internal", "internal"), ("trumpable", "trumpable"))
        if attrs.get(key) is True
    ]
    text = f"{name} [{attrs.get('type') or '?'} {_gib(attrs.get('size'))}]"
    if flags:
        text += " {" + ", ".join(flags) + "}"
    return f"{text} {link}".strip()


def report_rows(
    state: Mapping[str, Any],
    *,
    only_keys: Iterable[str] | None = None,
    needle: str = "",
    lint: Mapping[str, Sequence[Any]] | None = None,
) -> list[dict[str, str]]:
    releases = state.get("releases")
    releases = releases if isinstance(releases, Mapping) else {}
    keys = set(only_keys) if only_keys is not None else None
    wanted = needle.casefold()
    rows: list[dict[str, str]] = []
    for key, record in releases.items():
        if not isinstance(record, Mapping) or (keys is not None and key not in keys):
            continue
        name = str(record.get("name") or key)
        if wanted and wanted not in name.casefold():
            continue
        rows.append(
            {
                "status": str(record.get("status") or "?"),
                "reason": str(record.get("reason") or ""),
                "name": name,
                "tmdb": str(record.get("tmdb_id") or ""),
                "imdb": str(record.get("imdb_id") or ""),
                "aither_match": _match_text(record),
                "message": str(record.get("message") or ""),
                "lint": "; ".join(
                    str(getattr(item, "message", item)) for item in (lint or {}).get(name, ())
                ),
            }
        )

    def order(row: Mapping[str, str]) -> tuple[int, str]:
        status = row["status"]
        index = STATUS_ORDER.index(status) if status in STATUS_ORDER else len(STATUS_ORDER)
        return index, row["name"].casefold()

    rows.sort(key=order)
    return rows


def format_table(
    rows: Sequence[Mapping[str, str]], *, verbose: bool = False, limit_name: int = 70
) -> str:
    if not rows:
        return "(nincs találat)"
    lines: list[str] = []
    for row in rows:
        name = row["name"]
        if not verbose and len(name) > limit_name:
            name = name[: limit_name - 1] + "…"
        marker = "  [lint]" if row.get("lint") else ""
        lines.append(f"{row['status']:<11} {row['reason'][:34]:<34} {name}{marker}")
        if row.get("lint"):
            lines.append(f"    lint: {row['lint']}")
        if verbose:
            if row["tmdb"] or row["imdb"]:
                lines.append(f"    azonosító: tmdb={row['tmdb'] or '-'} imdb={row['imdb'] or '-'}")
            if row["aither_match"]:
                lines.append(f"    Aither-egyezés: {row['aither_match']}")
            if row["message"]:
                lines.append(f"    ok: {row['message']}")
    return "\n".join(lines)


def write_tsv(rows: Sequence[Mapping[str, str]], path: Path) -> None:
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
