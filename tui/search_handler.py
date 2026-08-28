"""SQLite 搜索索引 — 为已抓取文章提供快速搜索。

实现：
- 初始化 SQLite 数据库 search_index.db
- 表 articles (id, title, summary, link, source, publish_time, output_file)
- index_article(article): 插入或忽略重复 link
- search(query, source=None): 在 title/summary 中模糊匹配
- clear_stale(): 删除 output_file 已不存在的记录
"""

import logging
import os
import sqlite3
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "search_index.db")

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    summary TEXT,
    link TEXT UNIQUE,
    source TEXT,
    publish_time TEXT,
    output_file TEXT
)
"""


class SearchHandler:
    """SQLite 搜索索引管理."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or DB_PATH
        self._init_db()

    def _init_db(self) -> None:
        """初始化数据库和表."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(CREATE_TABLE_SQL)
            # 创建索引
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_articles_publish_time ON articles(publish_time)"
            )
            conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def index_article(self, article: dict[str, Any]) -> bool:
        """索引一篇文章（重复 link 则忽略）.

        Returns:
            True 如果插入成功，False 如果重复
        """
        try:
            with self._get_conn() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO articles
                       (title, summary, link, source, publish_time, output_file)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        article.get("title", ""),
                        article.get("summary", ""),
                        article.get("link", ""),
                        article.get("source", ""),
                        article.get("publish_time", ""),
                        article.get("output_file", ""),
                    ),
                )
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error("Failed to index article: %s", e)
            return False

    def search(self, query: str, source: str | None = None) -> list[dict[str, Any]]:
        """在已索引文章中搜索.

        Args:
            query: 搜索关键词
            source: 限定信源名称（可选）

        Returns:
            匹配的文章列表
        """
        try:
            with self._get_conn() as conn:
                sql = """SELECT * FROM articles
                          WHERE (title LIKE ? OR summary LIKE ?)"""
                params: list[Any] = [f"%{query}%", f"%{query}%"]

                if source:
                    sql += " AND source = ?"
                    params.append(source)

                sql += " ORDER BY publish_time DESC LIMIT 50"
                cursor = conn.execute(sql, params)
                return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error("Search failed: %s", e)
            return []

    def clear_stale(self) -> int:
        """清理孤立记录（对应 output_file 已删除）.

        Returns:
            删除的记录数
        """
        output_dir = os.path.join(os.path.dirname(__file__), "..", "output")
        try:
            with self._get_conn() as conn:
                cursor = conn.execute("SELECT id, output_file FROM articles")
                stale_ids = []
                for row in cursor.fetchall():
                    filepath = os.path.join(output_dir, row["output_file"])
                    if not os.path.exists(filepath):
                        stale_ids.append(row["id"])

                if stale_ids:
                    placeholders = ",".join("?" * len(stale_ids))
                    conn.execute(
                        f"DELETE FROM articles WHERE id IN ({placeholders})",
                        stale_ids,
                    )
                    conn.commit()
                    logger.info("Cleared %d stale records", len(stale_ids))
                return len(stale_ids)
        except sqlite3.Error as e:
            logger.error("Failed to clear stale records: %s", e)
            return 0

    def get_all(self, limit: int = 100) -> list[dict[str, Any]]:
        """获取所有索引记录."""
        try:
            with self._get_conn() as conn:
                cursor = conn.execute(
                    "SELECT * FROM articles ORDER BY publish_time DESC LIMIT ?",
                    (limit,),
                )
                return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error("Failed to get all records: %s", e)
            return []