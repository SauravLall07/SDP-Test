from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from app import create_app
from rat.db import connect, initialize
from rat.git_analyzer import analyze_repository
from rat.metrics import (
    association_metrics,
    bus_factor,
    coverage,
    gini,
    hhi,
    hotspot_table,
    linear_trend,
    pareto_curve,
)


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

    def test_metric_endpoints_timeline_insights_and_coupling(self) -> None:
        app = create_app(str(self.data), testing=True)
        client = app.test_client()

        timeline = client.get(f"/api/repositories/{self.repository_id}/timeline").get_json()
        self.assertEqual(timeline["bucket"], "week")
        self.assertEqual(len(timeline["points"]), 1)
        week = timeline["points"][0]
        self.assertEqual((week["commits"], week["added"], week["removed"]), (6, 5, 1))
        self.assertEqual(week["cumulative_net"], 4)
        self.assertEqual(timeline["trend"]["n"], 1)

        monthly = client.get(
            f"/api/repositories/{self.repository_id}/timeline", query_string={"bucket": "month"}
        ).get_json()
        self.assertEqual(len(monthly["points"]), 1)
        self.assertEqual(len(monthly["points"][0]["label"]), 7)
        self.assertEqual(monthly["points"][0]["added"], 5)

        authors = client.get(f"/api/repositories/{self.repository_id}/authors").get_json()
        alice = next(author for author in authors if author["name"] == "Alice")
        scoped = client.get(
            f"/api/repositories/{self.repository_id}/timeline", query_string={"author_id": alice["id"]}
        ).get_json()
        self.assertEqual(scoped["points"][0]["commits"], 4)

        insights = client.get(f"/api/repositories/{self.repository_id}/insights").get_json()
        self.assertEqual(insights["total_commits"], 6)
        self.assertEqual(insights["authors"]["items"], 2)
        self.assertAlmostEqual(insights["authors"]["gini"], 1 / 3, places=4)
        self.assertEqual(insights["authors"]["bus_factor"], 1)
        self.assertEqual(insights["files"]["coverage_80"]["items"], 3)
        self.assertEqual(insights["hotspots"][0]["churn"], 2)

        coupling = client.get(f"/api/repositories/{self.repository_id}/coupling").get_json()
        self.assertEqual(len(coupling["files"]), 4)
        self.assertIn("pairs", coupling)

        bad_bucket = client.get(
            f"/api/repositories/{self.repository_id}/timeline", query_string={"bucket": "hour"}
        )
        self.assertEqual(bad_bucket.status_code, 400)

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


class MetricsTestCase(unittest.TestCase):
    def test_gini_coefficient(self) -> None:
        self.assertEqual(gini([]), 0)
        self.assertEqual(gini([1, 1, 1, 1]), 0)
        self.assertAlmostEqual(gini([0, 10]), 0.5)
        self.assertAlmostEqual(gini(list(range(1, 11))), 0.3)

    def test_herfindahl_hirschman_index(self) -> None:
        self.assertEqual(hhi([]), 0)
        self.assertEqual(hhi([5]), 1)
        self.assertAlmostEqual(hhi([1, 1, 1, 1]), 0.25)
        self.assertAlmostEqual(hhi([2, 2]), 0.5)

    def test_bus_factor_and_coverage(self) -> None:
        self.assertEqual(bus_factor([]), 0)
        self.assertEqual(bus_factor([5, 3, 2]), 1)
        self.assertEqual(bus_factor([1, 1, 1, 1]), 2)
        self.assertEqual(bus_factor([1, 1, 1, 1, 1]), 3)
        self.assertEqual(coverage([8, 1, 1], 0.8), {"items": 1, "total_items": 3, "share": 1 / 3})
        self.assertEqual(coverage([2, 2, 1, 1], 0.8)["items"], 3)

    def test_pareto_curve_is_monotonic(self) -> None:
        curve = pareto_curve([4, 3, 2, 1], points=2)
        self.assertEqual(curve, [[0.0, 0.0], [0.5, 0.7], [1.0, 1.0]])
        self.assertEqual(pareto_curve([], points=2), [[0.0, 0.0], [1.0, 0.0]])

    def test_linear_trend(self) -> None:
        trend = linear_trend([1, 2, 3, 4])
        self.assertAlmostEqual(trend["slope"], 1.0)
        self.assertAlmostEqual(trend["intercept"], 1.0)
        self.assertAlmostEqual(trend["r2"], 1.0)
        flat = linear_trend([2, 2, 2])
        self.assertEqual((flat["slope"], flat["r2"]), (0.0, 0.0))
        self.assertEqual(linear_trend([])["n"], 0)

    def test_association_metrics(self) -> None:
        result = association_metrics(6, 10, 12, 50)
        self.assertAlmostEqual(result["confidence"], 0.6)
        self.assertAlmostEqual(result["lift"], 2.5)
        self.assertAlmostEqual(result["support"], 0.12)
        self.assertEqual(association_metrics(0, 10, 12, 50)["lift"], 0.0)

    def test_hotspot_ranking(self) -> None:
        ranked = hotspot_table([
            {"path": "a", "churn": 4, "churn_rate": 1.0, "modification_frequency": 0.3},
            {"path": "b", "churn": 9, "churn_rate": 2.0, "modification_frequency": 0.5},
        ])
        self.assertEqual([item["path"] for item in ranked], ["b", "a"])
        self.assertAlmostEqual(ranked[0]["score"], 1.0)


if __name__ == "__main__":
    unittest.main()
