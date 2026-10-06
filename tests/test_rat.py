from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from app import create_app
from rat.db import connect, initialize
from rat.git_analyzer import analyze_repository


def git(repository: Path, *arguments: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, **(env or {})},
    )
    return result.stdout.strip()


class RatTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "source"
        self.repository.mkdir()
        git(self.repository, "init", "-q")
        git(self.repository, "config", "user.name", "Alice")
        git(self.repository, "config", "user.email", "alice@example.com")
        self._build_history()
        self.data = self.root / "data"
        self.db_path = self.data / "rat.sqlite3"
        initialize(self.db_path)
        with connect(self.db_path) as connection:
            cursor = connection.execute(
                """INSERT INTO repositories(name, source_type, source, local_path)
                   VALUES ('Fixture', 'zip', 'test', ?)""",
                (str(self.repository),),
            )
            self.repository_id = int(cursor.lastrowid)
        analyze_repository(str(self.db_path), self.repository_id)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _commit(self, message: str, name: str, email: str) -> str:
        git(self.repository, "add", "-A")
        environment = {
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
        }
        git(self.repository, "commit", "-q", "-m", message, env=environment)
        return git(self.repository, "rev-parse", "HEAD")

    def _build_history(self) -> None:
        (self.repository / "src").mkdir()
        (self.repository / "src" / "app.txt").write_text("a\nb\n")
        self.initial_sha = self._commit("initial", "Alias", "alias@example.com")

        (self.repository / "docs").mkdir()
        (self.repository / "docs" / "readme.md").write_text("docs\n")
        self.docs_sha = self._commit("docs", "Bob", "bob@example.com")

        (self.repository / "lib").mkdir()
        (self.repository / "src" / "app.txt").rename(self.repository / "lib" / "app.txt")
        (self.repository / "lib" / "app.txt").write_text("a\nb\nc\n")
        self.rename_sha = self._commit("rename and change", "Alice", "alice@example.com")

        (self.repository / "lib" / "app.txt").write_text("a\nc\n")
        self.remove_sha = self._commit("remove line", "Alias", "alias@example.com")

        (self.repository / "image.bin").write_bytes(b"\x00\x01\x02\x03")
        self.binary_sha = self._commit("binary", "Bob", "bob@example.com")

        (self.repository / ".mailmap").write_text(
            "Alice <alice@example.com> Alias <alias@example.com>\n"
        )
        self.mailmap_sha = self._commit("mailmap", "Alice", "alice@example.com")

    def test_complete_metric_formulas_and_git_semantics(self) -> None:
        with connect(self.db_path) as connection:
            repository = connection.execute(
                "SELECT status, commit_count FROM repositories WHERE id=?",
                (self.repository_id,),
            ).fetchone()
            self.assertEqual(dict(repository), {"status": "ready", "commit_count": 6})
            root = connection.execute(
                """SELECT SUM(ch.added) added, SUM(ch.removed) removed,
                          SUM(ch.added + ch.removed) churn
                   FROM changes ch JOIN commits c ON c.id=ch.commit_id
                   WHERE c.repository_id=? AND ch.path='/' AND ch.kind='directory'""",
                (self.repository_id,),
            ).fetchone()
            self.assertEqual((root["added"], root["removed"], root["churn"]), (5, 1, 6))
            renamed = connection.execute(
                """SELECT SUM(ch.added), SUM(ch.removed)
                   FROM changes ch JOIN commits c ON c.id=ch.commit_id
                   WHERE c.repository_id=? AND ch.path='lib/app.txt' AND ch.kind='file'""",
                (self.repository_id,),
            ).fetchone()
            self.assertEqual(tuple(renamed), (1, 1))
            binary = connection.execute(
                """SELECT COUNT(*) FROM changes ch JOIN commits c ON c.id=ch.commit_id
                   WHERE c.repository_id=? AND ch.path='image.bin'""",
                (self.repository_id,),
            ).fetchone()[0]
            self.assertEqual(binary, 0)
            authors = connection.execute(
                "SELECT name, email, COUNT(c.id) commits FROM authors a JOIN commits c ON c.author_id=a.id WHERE a.repository_id=? GROUP BY a.id ORDER BY name",
                (self.repository_id,),
            ).fetchall()
            self.assertEqual([(row["name"], row["email"], row["commits"]) for row in authors], [
                ("Alice", "alice@example.com", 4), ("Bob", "bob@example.com", 2)
            ])

        analyze_repository(str(self.db_path), self.repository_id)
        with connect(self.db_path) as connection:
            counts = connection.execute(
                "SELECT commit_count, status FROM repositories WHERE id=?", (self.repository_id,)
            ).fetchone()
            self.assertEqual((counts["commit_count"], counts["status"]), (6, "ready"))

    def test_analytics_filters_and_author_ownership(self) -> None:
        app = create_app(str(self.data), testing=True)
        client = app.test_client()
        response = client.get(f"/api/repositories/{self.repository_id}/analytics")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["commit_count"], 6)
        self.assertEqual(payload["summary"]["growth"], 4)
        self.assertAlmostEqual(payload["authors"][0]["ownership"], 5 / 6)

        response = client.get(
            f"/api/repositories/{self.repository_id}/analytics",
            query_string={"commits": f"{self.rename_sha},{self.remove_sha}", "path": "lib/app.txt", "kind": "file"},
        )
        payload = response.get_json()
        self.assertEqual(payload["commit_count"], 2)
        self.assertEqual(payload["objects"][0]["added"], 1)
        self.assertEqual(payload["objects"][0]["removed"], 1)
        self.assertEqual(payload["objects"][0]["modifications"], 2)
        self.assertEqual(payload["objects"][0]["modification_frequency"], 1)
        self.assertEqual(payload["objects"][0]["churn_rate"], 1)

    def test_manual_author_merge(self) -> None:
        app = create_app(str(self.data), testing=True)
        client = app.test_client()
        authors = client.get(f"/api/repositories/{self.repository_id}/authors").get_json()
        alice = next(author for author in authors if author["name"] == "Alice")
        bob = next(author for author in authors if author["name"] == "Bob")
        response = client.post(
            f"/api/repositories/{self.repository_id}/authors/merge",
            json={"source_id": bob["id"], "target_id": alice["id"]},
        )
        self.assertEqual(response.status_code, 200)
        merged = client.get(f"/api/repositories/{self.repository_id}/authors").get_json()
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["commits"], 6)


if __name__ == "__main__":
    unittest.main()
