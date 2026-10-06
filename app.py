from __future__ import annotations

import os
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from rat.db import connect, initialize
from rat.services import RepositoryService

BASE_DIR = Path(__file__).resolve().parent


def create_app(data_dir: str | None = None, testing: bool = False) -> Flask:
    app = Flask(__name__)
    root = Path(data_dir or os.environ.get("RAT_DATA_DIR", BASE_DIR / "data"))
    root.mkdir(parents=True, exist_ok=True)
    db_path = str(root / "rat.sqlite3")
    initialize(db_path)
    service = RepositoryService(db_path, str(root / "repositories"), workers=1 if testing else 2)
    app.config.update(
        DATABASE=db_path,
        DATA_DIR=str(root),
        MAX_CONTENT_LENGTH=1024 * 1024 * 1024,
        TESTING=testing,
        REPOSITORY_SERVICE=service,
    )

    def database():
        return connect(app.config["DATABASE"])

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify(error="ZIP upload exceeds 1 GiB"), 413

    @app.errorhandler(ValueError)
    def bad_request(error):
        return jsonify(error=str(error)), 400

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/repositories")
    def repositories():
        with database() as connection:
            rows = connection.execute(
                """SELECT id, name, source_type, source, status, error, head_sha,
                          commit_count, created_at, analyzed_at
                   FROM repositories ORDER BY id DESC"""
            ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.post("/api/repositories/clone")
    def clone_repository():
        payload = request.get_json(silent=True) or {}
        if not payload.get("url"):
            raise ValueError("Repository URL is required")
        repository_id = service.add_clone(payload.get("name", ""), payload["url"])
        return jsonify(id=repository_id, status="queued"), 202

    @app.post("/api/repositories/upload")
    def upload_repository():
        upload = request.files.get("archive")
        if upload is None or not upload.filename:
            raise ValueError("A ZIP archive is required")
        if not upload.filename.lower().endswith(".zip"):
            raise ValueError("Only .zip archives are accepted")
        incoming = root / "incoming"
        incoming.mkdir(exist_ok=True)
        archive_path = incoming / f"{uuid.uuid4().hex}.zip"
        upload.save(archive_path)
        repository_id = service.add_zip(request.form.get("name", ""), str(archive_path))
        return jsonify(id=repository_id, status="queued"), 202

    @app.post("/api/repositories/<int:repository_id>/reanalyze")
    def reanalyze(repository_id: int):
        service.reanalyze(repository_id)
        return jsonify(status="queued"), 202

    @app.delete("/api/repositories/<int:repository_id>")
    def delete_repository(repository_id: int):
        if not service.delete(repository_id):
            return jsonify(error="Repository not found"), 404
        return "", 204

    @app.get("/api/repositories/<int:repository_id>/authors")
    def authors(repository_id: int):
        with database() as connection:
            rows = connection.execute(
                """SELECT a.id, a.name, a.email, COUNT(c.id) AS commits
                   FROM authors a LEFT JOIN commits c ON c.author_id=a.id
                   WHERE a.repository_id=? GROUP BY a.id ORDER BY commits DESC, a.name""",
                (repository_id,),
            ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.post("/api/repositories/<int:repository_id>/authors/merge")
    def merge_authors(repository_id: int):
        payload = request.get_json(silent=True) or {}
        try:
            source_id = int(payload["source_id"])
            target_id = int(payload["target_id"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Source and target authors are required")
        if source_id == target_id:
            raise ValueError("Choose two different authors")
        with database() as connection:
            matches = connection.execute(
                "SELECT id FROM authors WHERE repository_id=? AND id IN (?, ?)",
                (repository_id, source_id, target_id),
            ).fetchall()
            if len(matches) != 2:
                raise ValueError("Both authors must belong to this repository")
            connection.execute("UPDATE commits SET author_id=? WHERE author_id=?", (target_id, source_id))
            connection.execute("DELETE FROM authors WHERE id=?", (source_id,))
        return jsonify(status="merged")

    @app.get("/api/repositories/<int:repository_id>/commits")
    def commits(repository_id: int):
        limit = min(max(request.args.get("limit", 250, type=int), 1), 1000)
        search = request.args.get("search", "").strip()
        parameters: list[object] = [repository_id]
        condition = ""
        if search:
            condition = "AND (c.sha LIKE ? OR a.name LIKE ? OR a.email LIKE ?)"
            value = f"%{search}%"
            parameters.extend([value, value, value])
        parameters.append(limit)
        with database() as connection:
            rows = connection.execute(
                f"""SELECT c.sha, c.committer_date, a.id AS author_id, a.name, a.email
                    FROM commits c JOIN authors a ON a.id=c.author_id
                    WHERE c.repository_id=? {condition}
                    ORDER BY c.committer_date DESC, c.id DESC LIMIT ?""",
                parameters,
            ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.get("/api/repositories/<int:repository_id>/objects")
    def objects(repository_id: int):
        search = request.args.get("search", "").strip()
        parameters: list[object] = [repository_id]
        condition = ""
        if search:
            condition = "AND ch.path LIKE ?"
            parameters.append(f"%{search}%")
        with database() as connection:
            rows = connection.execute(
                f"""SELECT ch.path, ch.kind, SUM(ch.added + ch.removed) AS churn
                    FROM changes ch JOIN commits c ON c.id=ch.commit_id
                    WHERE c.repository_id=? {condition}
                    GROUP BY ch.path, ch.kind ORDER BY churn DESC, ch.path LIMIT 1000""",
                parameters,
            ).fetchall()
        return jsonify([dict(row) for row in rows])

    @app.get("/api/repositories/<int:repository_id>/analytics")
    def analytics(repository_id: int):
        clauses = ["c.repository_id=?"]
        parameters: list[object] = [repository_id]
        commit_shas = [value for value in request.args.get("commits", "").split(",") if value]
        author_id = request.args.get("author_id", type=int)
        if commit_shas:
            placeholders = ",".join("?" for _ in commit_shas)
            clauses.append(f"c.sha IN ({placeholders})")
            parameters.extend(commit_shas)
        else:
            start = request.args.get("start", type=int)
            end = request.args.get("end", type=int)
            if start is not None:
                clauses.append("c.committer_date>=?")
                parameters.append(start)
            if end is not None:
                clauses.append("c.committer_date<?")
                parameters.append(end)
        if author_id is not None:
            clauses.append("c.author_id=?")
            parameters.append(author_id)
        where = " AND ".join(clauses)
        path = request.args.get("path", "").strip()
        kind = request.args.get("kind", "").strip()
        object_clauses: list[str] = []
        object_parameters: list[object] = []
        if path:
            object_clauses.append("ch.path=?")
            object_parameters.append(path)
        if kind in {"file", "directory"}:
            object_clauses.append("ch.kind=?")
            object_parameters.append(kind)
        object_where = " AND " + " AND ".join(object_clauses) if object_clauses else ""

        with database() as connection:
            repository = connection.execute(
                "SELECT id, name, status, head_sha FROM repositories WHERE id=?", (repository_id,)
            ).fetchone()
            if repository is None:
                return jsonify(error="Repository not found"), 404
            commit_count = connection.execute(
                f"SELECT COUNT(*) FROM commits c WHERE {where}", parameters
            ).fetchone()[0]
            rows = connection.execute(
                f"""SELECT ch.path, ch.kind, SUM(ch.added) AS added,
                           SUM(ch.removed) AS removed,
                           SUM(ch.added-ch.removed) AS growth,
                           SUM(ch.added+ch.removed) AS churn,
                           COUNT(DISTINCT CASE WHEN ch.added+ch.removed>0 THEN c.id END) AS modifications
                    FROM changes ch JOIN commits c ON c.id=ch.commit_id
                    WHERE {where} {object_where}
                    GROUP BY ch.path, ch.kind
                    ORDER BY churn DESC, ch.path LIMIT 1000""",
                [*parameters, *object_parameters],
            ).fetchall()
            objects_result = []
            for row in rows:
                item = dict(row)
                item["modification_frequency"] = item["modifications"] / commit_count if commit_count else 0
                item["churn_rate"] = item["churn"] / commit_count if commit_count else 0
                objects_result.append(item)

            focus_path = path or "/"
            focus_kind = kind if kind in {"file", "directory"} else ("directory" if not path else None)
            author_extra = " AND ch.path=?"
            author_parameters = [*parameters, focus_path]
            if focus_kind:
                author_extra += " AND ch.kind=?"
                author_parameters.append(focus_kind)
            author_rows = connection.execute(
                f"""SELECT a.id, a.name, a.email,
                           SUM(ch.added+ch.removed) AS churn,
                           COUNT(DISTINCT CASE WHEN ch.added+ch.removed>0 THEN c.id END) AS modifications
                    FROM changes ch JOIN commits c ON c.id=ch.commit_id
                    JOIN authors a ON a.id=c.author_id
                    WHERE {where} {author_extra}
                    GROUP BY a.id ORDER BY churn DESC, a.name""",
                author_parameters,
            ).fetchall()
            total_churn = sum(row["churn"] for row in author_rows)
            authors_result = [
                {**dict(row), "ownership": row["churn"] / total_churn if total_churn else 0}
                for row in author_rows
            ]
            root = next(
                (item for item in objects_result if item["path"] == "/" and item["kind"] == "directory"),
                None,
            )
        return jsonify(
            repository=dict(repository),
            commit_count=commit_count,
            summary=root,
            objects=objects_result,
            authors=authors_result,
            focus={"path": focus_path, "kind": focus_kind},
        )

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
