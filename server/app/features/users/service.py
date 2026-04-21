import json
import re

from app.db.connection import transaction
from app.features.papers.service import paper_to_api
from app.features.tools.llm import ChatMessage, LLMClient


def ensure_user(user_id: str) -> None:
    with transaction() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO users(id, display_name, home_categories, preference_profile)
            VALUES(?, ?, '[]', '{}')
            """,
            (user_id, user_id),
        )


class UserPreferenceService:
    KEYWORDS = [
        "rag",
        "llm",
        "agent",
        "alignment",
        "multimodal",
        "retrieval",
        "reasoning",
        "robotics",
        "diffusion",
        "transformer",
        "benchmark",
    ]

    def update_from_text(self, user_id: str, text: str) -> dict:
        ensure_user(user_id)
        lowered = text.lower()
        found = [keyword for keyword in self.KEYWORDS if keyword in lowered]
        arxiv_categories = re.findall(r"\b(?:cs|stat|math|eess)\.[A-Z]{2}\b", text)
        with transaction() as connection:
            row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            profile = json.loads(row["preference_profile"] or "{}")
            keywords = set(profile.get("keywords", []))
            keywords.update(found)
            categories = set(json.loads(row["home_categories"] or "[]"))
            categories.update(arxiv_categories)
            profile["keywords"] = sorted(keywords)
            connection.execute(
                """
                UPDATE users SET preference_profile = ?, home_categories = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (json.dumps(profile, ensure_ascii=False), json.dumps(sorted(categories)), user_id),
            )
        return {"keywords": sorted(keywords), "categories": sorted(categories)}

    def settings(self, user_id: str) -> dict:
        ensure_user(user_id)
        with transaction() as connection:
            user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            stats = {
                "papers": connection.execute("SELECT COUNT(*) AS count FROM papers").fetchone()["count"],
                "parsedPapers": connection.execute("SELECT COUNT(*) AS count FROM papers WHERE markdown_path IS NOT NULL").fetchone()["count"],
                "chatMessages": connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM chat_messages m
                    JOIN chat_sessions s ON s.id = m.session_id
                    WHERE s.user_id = ?
                    """,
                    (user_id,),
                ).fetchone()["count"],
                "favorites": connection.execute(
                    "SELECT COUNT(*) AS count FROM paper_favorites WHERE user_id = ?",
                    (user_id,),
                ).fetchone()["count"],
            }
        profile = json.loads(user["preference_profile"] or "{}")
        return {
            "userId": user_id,
            "preferenceText": profile.get("preferenceText", ""),
            "keywords": profile.get("keywords", []),
            "homeCategories": json.loads(user["home_categories"] or "[]"),
            "stats": stats,
        }

    def update_preference_text(self, user_id: str, text: str) -> dict:
        ensure_user(user_id)
        with transaction() as connection:
            row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            profile = json.loads(row["preference_profile"] or "{}")
            profile["preferenceText"] = text.strip()
            connection.execute(
                "UPDATE users SET preference_profile = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (json.dumps(profile, ensure_ascii=False), user_id),
            )
        return self.settings(user_id)

    def clear_chat_memory(self, user_id: str) -> dict:
        ensure_user(user_id)
        with transaction() as connection:
            session_ids = [
                row["id"]
                for row in connection.execute("SELECT id FROM chat_sessions WHERE user_id = ?", (user_id,)).fetchall()
            ]
            for session_id in session_ids:
                connection.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM chat_sessions WHERE user_id = ?", (user_id,))
        return {"deletedSessions": len(session_ids)}

    def delete_unfavorited_papers(self, user_id: str) -> dict:
        ensure_user(user_id)
        with transaction() as connection:
            rows = connection.execute(
                """
                SELECT p.id
                FROM papers p
                WHERE NOT EXISTS (
                  SELECT 1 FROM paper_favorites f
                  WHERE f.paper_id = p.id AND f.user_id = ?
                )
                """,
                (user_id,),
            ).fetchall()
            paper_ids = [row["id"] for row in rows]
            if paper_ids:
                placeholders = ",".join("?" for _ in paper_ids)
                connection.execute(f"DELETE FROM papers WHERE id IN ({placeholders})", paper_ids)
        return {"deletedPapers": len(paper_ids)}

    def recommendations(self, user_id: str, limit: int = 10) -> list[dict]:
        ensure_user(user_id)
        with transaction() as connection:
            user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            profile = json.loads(user["preference_profile"] or "{}")
            categories = json.loads(user["home_categories"] or "[]")
            keywords = profile.get("keywords", [])

            where: list[str] = []
            params: list[object] = []
            if categories:
                category_terms = []
                for category in categories:
                    category_terms.append("(category = ? OR raw_metadata LIKE ?)")
                    params.extend([category, f'%"{category}"%'])
                where.append("(" + " OR ".join(category_terms) + ")")
            if keywords:
                keyword_sql = " OR ".join(["(title LIKE ? OR abstract LIKE ? OR ai_summary LIKE ?)"] * len(keywords))
                where.append(f"({keyword_sql})")
                for keyword in keywords:
                    term = f"%{keyword}%"
                    params.extend([term, term, term])
            sql = "SELECT * FROM papers"
            if where:
                sql += " WHERE " + " OR ".join(where)
            sql += " ORDER BY COALESCE(analyzed_at, updated_at, published_at, created_at) DESC LIMIT ?"
            params.append(limit)
            rows = connection.execute(sql, params).fetchall()

            if not rows:
                rows = connection.execute(
                    "SELECT * FROM papers ORDER BY COALESCE(updated_at, published_at, created_at) DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [paper_to_api(row) for row in rows]

    async def ai_recommendations(self, user_id: str, limit: int = 10) -> list[dict]:
        ensure_user(user_id)
        with transaction() as connection:
            user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            rows = connection.execute(
                """
                SELECT * FROM papers
                ORDER BY COALESCE(analyzed_at, updated_at, published_at, created_at) DESC
                LIMIT 40
                """
            ).fetchall()
        profile = json.loads(user["preference_profile"] or "{}")
        preference_text = profile.get("preferenceText", "")
        papers = [paper_to_api(row) for row in rows]
        compact = [
            {
                "id": paper["id"],
                "arxivId": paper["arxivId"],
                "title": paper["title"],
                "summary": (paper["aiSummary"] or paper["abstract"])[:900],
                "tags": paper["tags"],
            }
            for paper in papers
        ]
        if not compact:
            return []

        response = await LLMClient().complete(
            "ai-paper-recommendations",
            [
                ChatMessage(
                    "system",
                    "You recommend papers. Return strict JSON array. Each item: {id:number, reason:string, reasonTags:string[]}. Use Chinese reasonTags.",
                ),
                ChatMessage(
                    "user",
                    f"User preference in natural language:\n{preference_text or '用户尚未填写偏好，请按论文质量和新近程度推荐。'}\n\nCandidate papers:\n{json.dumps(compact, ensure_ascii=False)}\n\nReturn top {limit}.",
                ),
            ],
            use_cache=False,
        )
        recommendations: list[dict] = []
        try:
            start = response.index("[")
            end = response.rindex("]") + 1
            recommendations = json.loads(response[start:end])
        except (ValueError, json.JSONDecodeError):
            recommendations = []

        reason_by_id = {
            int(item.get("id")): {
                "recommendationReason": item.get("reason", "匹配当前研究偏好"),
                "recommendationTags": item.get("reasonTags", ["偏好匹配"]),
            }
            for item in recommendations
            if item.get("id") is not None
        }
        ranked: list[dict] = []
        for item in recommendations:
            paper_id = item.get("id")
            paper = next((paper for paper in papers if paper["id"] == paper_id), None)
            if paper:
                paper.update(reason_by_id.get(paper["id"], {}))
                ranked.append(paper)
        if not ranked:
            ranked = self.recommendations(user_id, limit)
            for paper in ranked:
                paper["recommendationReason"] = "根据当前论文摘要、标签和偏好粗略匹配。"
                paper["recommendationTags"] = ["本地匹配"]
        return ranked[:limit]

    def favorite_folders(self, user_id: str) -> list[dict]:
        ensure_user(user_id)
        with transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM favorite_folders WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [{"id": row["id"], "name": row["name"], "createdAt": row["created_at"]} for row in rows]

    def create_folder(self, user_id: str, name: str) -> dict:
        ensure_user(user_id)
        clean_name = name.strip()[:80]
        if not clean_name:
            clean_name = "默认收藏"
        with transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO favorite_folders(user_id, name)
                VALUES(?, ?)
                """,
                (user_id, clean_name),
            )
            row = connection.execute(
                "SELECT * FROM favorite_folders WHERE user_id = ? AND name = ?",
                (user_id, clean_name),
            ).fetchone()
        return {"id": row["id"], "name": row["name"], "createdAt": row["created_at"]}

    def favorite_paper(self, user_id: str, paper_id: int, folder_id: int | None = None) -> dict:
        ensure_user(user_id)
        if folder_id is None:
            folder = self.create_folder(user_id, "默认收藏")
            folder_id = folder["id"]
        with transaction() as connection:
            connection.execute(
                """
                INSERT INTO paper_favorites(user_id, paper_id, folder_id)
                VALUES(?, ?, ?)
                ON CONFLICT(user_id, paper_id) DO UPDATE SET folder_id = excluded.folder_id
                """,
                (user_id, paper_id, folder_id),
            )
        return {"paperId": paper_id, "folderId": folder_id}

    def favorite_papers(self, user_id: str, folder_id: int | None = None, limit: int = 50) -> list[dict]:
        ensure_user(user_id)
        params: list[object] = [user_id]
        sql = """
            SELECT p.*
            FROM paper_favorites f
            JOIN papers p ON p.id = f.paper_id
            WHERE f.user_id = ?
        """
        if folder_id is not None:
            sql += " AND f.folder_id = ?"
            params.append(folder_id)
        sql += " ORDER BY f.created_at DESC LIMIT ?"
        params.append(limit)
        with transaction() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [paper_to_api(row) for row in rows]
