from __future__ import annotations

import os
import subprocess
from collections import defaultdict
from collections.abc import Iterator
from pathlib import PurePosixPath

from .db import connect

RECORD_SEPARATOR = b"\x1e"
FIELD_SEPARATOR = b"\x1f"


class AnalysisError(RuntimeError):
    pass


def run_git(repo_path: str, *arguments: str) -> str:
    process = subprocess.run(
        ["git", "-C", repo_path, *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        timeout=300,
        env={**os.environ, "LC_ALL": "C"},
    )
    if process.returncode:
        raise AnalysisError(process.stderr.strip() or "Git command failed")
    return process.stdout.strip()


def _record_stream(repo_path: str, reference: str) -> Iterator[bytes]:
    command = [
        "git", "-C", repo_path, "-c", "core.quotepath=false", "log",
        "--use-mailmap", "--no-merges", "--reverse", "--root", "-M50%",
        "--format=%x1e%H%x1f%ct%x1f%aN%x1f%aE%x00", "--numstat", "-z",
        reference, "--",
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "LC_ALL": "C"},
    )
    assert process.stdout is not None
    buffer = b""
    while chunk := process.stdout.read(1024 * 1024):
        buffer += chunk
        records = buffer.split(RECORD_SEPARATOR)
        buffer = records.pop()
        for record in records:
            if record:
                yield record
    if buffer:
        yield buffer
    process.stdout.close()
    stderr = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
    return_code = process.wait()
    if process.stderr:
        process.stderr.close()
    if return_code != 0:
        raise AnalysisError(stderr.strip() or "Unable to read Git history")


def _decode(value: bytes) -> str:
    return value.decode("utf-8", "surrogateescape")


def _parse_record(record: bytes) -> tuple[str, int, str, str, list[tuple[str, int, int]]]:
    header, separator, payload = record.partition(b"\x00")
    if not separator:
        raise AnalysisError("Malformed Git history record")
    fields = header.strip(b"\n").split(FIELD_SEPARATOR)
    if len(fields) != 4:
        raise AnalysisError("Malformed Git commit metadata")
    sha, timestamp, author_name, author_email = fields
    changes: list[tuple[str, int, int]] = []
    tokens = payload.split(b"\x00")
    index = 0
    while index < len(tokens):
        token = tokens[index].lstrip(b"\n")
        index += 1
        if not token:
            continue
        parts = token.split(b"\t", 2)
        if len(parts) != 3:
            continue
        added_raw, removed_raw, path_raw = parts
        if added_raw == b"-" or removed_raw == b"-":
            if not path_raw and index + 1 < len(tokens):
                index += 2
            continue
        try:
            added, removed = int(added_raw), int(removed_raw)
        except ValueError:
            continue
        if path_raw:
            path = _decode(path_raw)
        elif index + 1 < len(tokens):
            # With -z, a rename is encoded as an empty path followed by old and new paths.
            index += 1  # The old path is intentionally not attributed.
            path = _decode(tokens[index])
            index += 1
        else:
            continue
        if path:
            changes.append((path, added, removed))
    return _decode(sha), int(timestamp), _decode(author_name), _decode(author_email), changes


def directory_paths(file_path: str) -> list[str]:
    parent = PurePosixPath(file_path).parent
    directories = ["/"]
    if str(parent) == ".":
        return directories
    current: list[str] = []
    for part in parent.parts:
        current.append(part)
        directories.append("/".join(current))
    return directories


def analyze_repository(db_path: str, repository_id: int) -> None:
    connection = connect(db_path)
    try:
        repository = connection.execute(
            "SELECT * FROM repositories WHERE id = ?", (repository_id,)
        ).fetchone()
        if repository is None:
            raise AnalysisError("Repository no longer exists")
        repo_path = repository["local_path"]
        reference = repository["reference"]
        connection.execute(
            "UPDATE repositories SET status='analyzing', error=NULL WHERE id=?",
            (repository_id,),
        )
        connection.execute(
            "DELETE FROM commits WHERE repository_id=?", (repository_id,)
        )
        connection.execute(
            "DELETE FROM authors WHERE repository_id=?", (repository_id,)
        )
        connection.commit()

        head_sha = run_git(repo_path, "rev-parse", "--verify", reference)
        author_cache: dict[tuple[str, str], int] = {}
        commit_count = 0

        for raw_record in _record_stream(repo_path, reference):
            sha, timestamp, name, email, file_changes = _parse_record(raw_record)
            identity = (name.strip() or "Unknown", email.strip().lower())
            author_id = author_cache.get(identity)
            if author_id is None:
                cursor = connection.execute(
                    "INSERT INTO authors(repository_id, name, email) VALUES (?, ?, ?)",
                    (repository_id, *identity),
                )
                author_id = int(cursor.lastrowid)
                author_cache[identity] = author_id
            cursor = connection.execute(
                "INSERT INTO commits(repository_id, sha, committer_date, author_id) VALUES (?, ?, ?, ?)",
                (repository_id, sha, timestamp, author_id),
            )
            commit_id = int(cursor.lastrowid)
            directory_totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
            for path, added, removed in file_changes:
                connection.execute(
                    "INSERT INTO changes(commit_id, path, kind, added, removed) VALUES (?, ?, 'file', ?, ?)",
                    (commit_id, path, added, removed),
                )
                for directory in directory_paths(path):
                    directory_totals[directory][0] += added
                    directory_totals[directory][1] += removed
            connection.executemany(
                "INSERT INTO changes(commit_id, path, kind, added, removed) VALUES (?, ?, 'directory', ?, ?)",
                ((commit_id, path, totals[0], totals[1]) for path, totals in directory_totals.items()),
            )
            commit_count += 1
            if commit_count % 500 == 0:
                connection.commit()

        connection.execute(
            """UPDATE repositories
               SET status='ready', error=NULL, head_sha=?, commit_count=?, analyzed_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (head_sha, commit_count, repository_id),
        )
        connection.commit()
    except Exception as error:
        connection.rollback()
        connection.execute(
            "UPDATE repositories SET status='error', error=? WHERE id=?",
            (str(error)[:2000], repository_id),
        )
        connection.commit()
        raise
    finally:
        connection.close()
