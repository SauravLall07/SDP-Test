from __future__ import annotations

import shutil
import stat
import subprocess
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from .db import connect
from .git_analyzer import AnalysisError, analyze_repository, run_git


class RepositoryService:
    def __init__(self, db_path: str, storage_root: str, workers: int = 2) -> None:
        self.db_path = db_path
        self.storage_root = Path(storage_root)
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rat-analysis")
        self._lock = threading.Lock()

    def _create(self, name: str, source_type: str, source: str) -> tuple[int, Path]:
        safe_name = (name.strip() or "repository")[:120]
        with connect(self.db_path) as connection:
            cursor = connection.execute(
                "INSERT INTO repositories(name, source_type, source, local_path) VALUES (?, ?, ?, '')",
                (safe_name, source_type, source),
            )
            repository_id = int(cursor.lastrowid)
            target = self.storage_root / str(repository_id)
            connection.execute(
                "UPDATE repositories SET local_path=? WHERE id=?",
                (str(target), repository_id),
            )
        return repository_id, target

    def add_clone(self, name: str, url: str) -> int:
        value = url.strip()
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https", "ssh", "git"} or not parsed.hostname:
            raise ValueError("Use an http, https, ssh, or git repository URL")
        repository_id, target = self._create(name or Path(parsed.path).stem, "clone", value)
        self.executor.submit(self._clone_and_analyze, repository_id, value, target)
        return repository_id

    def add_zip(self, name: str, archive_path: str) -> int:
        repository_id, target = self._create(name or Path(archive_path).stem, "zip", "uploaded archive")
        self.executor.submit(self._extract_and_analyze, repository_id, Path(archive_path), target)
        return repository_id

    def _set_error(self, repository_id: int, error: Exception) -> None:
        with connect(self.db_path) as connection:
            connection.execute(
                "UPDATE repositories SET status='error', error=? WHERE id=?",
                (str(error)[:2000], repository_id),
            )

    def _clone_and_analyze(self, repository_id: int, url: str, target: Path) -> None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            process = subprocess.run(
                ["git", "clone", "--no-local", "--", url, str(target)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                timeout=1800,
            )
            if process.returncode:
                raise AnalysisError(process.stderr.strip() or "Clone failed")
            analyze_repository(self.db_path, repository_id)
        except Exception as error:
            self._set_error(repository_id, error)

    def _extract_and_analyze(self, repository_id: int, archive: Path, target: Path) -> None:
        try:
            target.mkdir(parents=True, exist_ok=True)
            expanded_size = 0
            with zipfile.ZipFile(archive) as bundle:
                members = bundle.infolist()
                if len(members) > 100_000:
                    raise ValueError("Archive contains too many entries")
                for member in members:
                    expanded_size += member.file_size
                    if expanded_size > 5 * 1024 * 1024 * 1024:
                        raise ValueError("Expanded archive exceeds 5 GiB")
                    destination = (target / member.filename).resolve()
                    if target.resolve() not in destination.parents and destination != target.resolve():
                        raise ValueError("Archive contains an unsafe path")
                    mode = member.external_attr >> 16
                    if stat.S_ISLNK(mode):
                        raise ValueError("Archive symlinks are not accepted")
                bundle.extractall(target)
            archive.unlink(missing_ok=True)

            candidates = sorted(
                (path.parent for path in target.rglob(".git")),
                key=lambda path: (len(path.parts), str(path)),
            )
            repository_path = next(
                (path for path in candidates if self._is_work_tree(path)), None
            )
            if repository_path is None:
                # No .git found — synthesize a single-commit repo from the source files.
                repository_path = self._find_content_root(target)
                self._init_git_repo(repository_path)
            with connect(self.db_path) as connection:
                connection.execute(
                    "UPDATE repositories SET local_path=? WHERE id=?",
                    (str(repository_path), repository_id),
                )
            analyze_repository(self.db_path, repository_id)
        except Exception as error:
            archive.unlink(missing_ok=True)
            self._set_error(repository_id, error)

    @staticmethod
    def _is_work_tree(path: Path) -> bool:
        try:
            return run_git(str(path), "rev-parse", "--is-inside-work-tree") == "true"
        except AnalysisError:
            return False

    @staticmethod
    def _find_content_root(target: Path) -> Path:
        """Find the shallowest directory that actually contains source files.

        GitHub ZIPs wrap everything in a single top-level folder like
        ``repo-main/``.  If the target contains exactly one subdirectory
        and no files of its own, descend into that subdirectory.
        """
        root = target
        while True:
            children = [child for child in root.iterdir() if not child.name.startswith(".")]
            dirs = [child for child in children if child.is_dir()]
            files = [child for child in children if child.is_file()]
            if len(dirs) == 1 and not files:
                root = dirs[0]
            else:
                break
        return root

    @staticmethod
    def _init_git_repo(path: Path) -> None:
        """Turn a plain directory into a single-commit Git repository."""
        import os
        env = {**os.environ, "LC_ALL": "C"}
        subprocess.run(
            ["git", "init"], cwd=str(path), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=True, env=env,
        )
        subprocess.run(
            ["git", "add", "."], cwd=str(path), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=True, env=env,
        )
        subprocess.run(
            ["git", "-c", "user.name=ZIP Upload", "-c", "user.email=upload@rat",
             "commit", "-m", "Initial snapshot from uploaded ZIP"],
            cwd=str(path), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=True, env=env,
        )

    def reanalyze(self, repository_id: int) -> None:
        with connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT local_path FROM repositories WHERE id=?", (repository_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Repository not found")
            connection.execute(
                "UPDATE repositories SET status='queued', error=NULL WHERE id=?",
                (repository_id,),
            )
        self.executor.submit(analyze_repository, self.db_path, repository_id)

    def delete(self, repository_id: int) -> bool:
        with self._lock, connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT local_path FROM repositories WHERE id=?", (repository_id,)
            ).fetchone()
            if row is None:
                return False
            connection.execute("DELETE FROM repositories WHERE id=?", (repository_id,))
        path = Path(row["local_path"])
        storage = self.storage_root.resolve()
        if path.exists():
            root = next((parent for parent in [path, *path.parents] if parent.parent == storage), None)
            if root is not None:
                shutil.rmtree(root, ignore_errors=True)
        return True
