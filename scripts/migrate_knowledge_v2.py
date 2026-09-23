"""Migrate `KnowledgeDoc` from "one doc per agent" to the domain-wide library model.

Old schema:  KnowledgeDoc(id, agent_id, filename, num_chunks, status, error, created_at)
New schema:  KnowledgeDoc(id, domain_id, filename, title, summary, scope, version,
                          num_chunks, status, error, created_at, updated_at)
             + AgentDoc(agent_id, doc_id)   -- the many-to-many link

SQLite can't ALTER a table this much, so this script: reads the old rows via raw
SQL, drops the table, lets `init_db()` recreate it with the new shape, then
re-ingests each *unique filename* exactly once from the original source file on
disk and relinks every agent that used to have its own copy. Content is re-read
from `domains/<domain>/knowledge/<filename>` rather than reconstructed from old
Chroma chunks — simpler, and produces identical text.

Idempotent: if the `knowledgedoc` table already has the new shape (a `domain_id`
column), the script does nothing and exits 0.

Chạy: python scripts/migrate_knowledge_v2.py [--db path/to/demo.db]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    return column in cols


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None, help="Đường dẫn demo.db (mặc định: settings.db_path)")
    parser.add_argument("--domain", default=None, help="domain_id để gán cho tài liệu cũ (mặc định: settings.domain_id)")
    args = parser.parse_args()

    from backend.app.config import settings

    db_path = Path(args.db) if args.db else settings.db_path
    domain_id = args.domain or settings.domain_id

    if not db_path.exists():
        print(f"Không có {db_path} — chưa có gì để migrate, coi như xong.")
        return 0

    conn = sqlite3.connect(str(db_path))
    try:
        if not _table_exists(conn, "knowledgedoc"):
            print("Chưa có bảng knowledgedoc — coi như xong.")
            return 0
        # `agent_id` là tín hiệu đáng tin duy nhất: một migration thật sự (DROP + tạo
        # lại bằng model mới) sẽ xóa hẳn cột này. Ngược lại, một lần lỡ gọi init_db()
        # với code mới nhắm vào DB cũ (ví dụ chạy quên set DB_PATH) chỉ ADD COLUMN các
        # cột mới — bảng khi đó có CẢ agent_id LẪN domain_id, dữ liệu cũ vẫn nguyên,
        # nhưng domain_id/title/summary/scope rỗng và bảng agentdoc trống trơn. Kiểm
        # tra domain_id không đủ; phải ưu tiên agent_id còn tồn tại hay không.
        if not _has_column(conn, "knowledgedoc", "agent_id"):
            print("knowledgedoc đã ở schema mới (không còn agent_id) — không cần migrate.")
            return 0
        if _has_column(conn, "knowledgedoc", "domain_id"):
            print("CẢNH BÁO: bảng có cả agent_id lẫn domain_id — dấu hiệu init_db() từng chạy nhầm "
                  "vào DB này với code mới (cột bị ADD nhưng chưa migrate dữ liệu thật). Sẽ vẫn "
                  "DROP và dựng lại đúng quy trình, không có gì mất vì agent_id vẫn còn nguyên.")

        old_rows = conn.execute(
            "SELECT id, agent_id, filename, num_chunks, status, error, created_at FROM knowledgedoc"
        ).fetchall()
        print(f"Đọc được {len(old_rows)} bản ghi cũ (mỗi bản ghi = 1 agent x 1 tài liệu).")

        # filename -> {"agents": [...], "created_at": earliest, "old_ids": [...]}
        by_filename: dict[str, dict] = {}
        for old_id, agent_id, filename, _n, _status, _err, created_at in old_rows:
            g = by_filename.setdefault(filename, {"agents": [], "created_at": created_at, "old_ids": []})
            if agent_id not in g["agents"]:
                g["agents"].append(agent_id)
            g["old_ids"].append(old_id)
            if created_at and created_at < g["created_at"]:
                g["created_at"] = created_at

        print(f"-> {len(by_filename)} tài liệu duy nhất theo tên tệp, sẽ mỗi tài liệu embed đúng 1 lần.")

        conn.execute("DROP TABLE knowledgedoc")
        conn.commit()
    finally:
        conn.close()

    # Recreate tables with the new schema (knowledgedoc + agentdoc, and anything else new).
    from backend.app.db import init_db, session_scope
    from backend.app.models import AgentDoc, KnowledgeDoc
    from backend.knowledge import library
    from backend.knowledge.chunker import chunk_text
    from backend.knowledge.loaders import is_markdown, load_text

    init_db()

    src_dir = ROOT / "domains" / domain_id / "knowledge"
    migrated, missing_files, link_count = 0, [], 0

    for filename, info in by_filename.items():
        path = src_dir / filename
        if not path.exists():
            print(f"  ✗ không tìm thấy tệp gốc {path} — bỏ qua tài liệu này, agent sẽ mất liên kết")
            missing_files.append(filename)
            continue

        raw = path.read_bytes()
        try:
            doc = library.create_doc(domain_id, filename, raw, scope="restricted")
        except library.LibraryError as exc:
            print(f"  ✗ {filename}: {exc}")
            continue

        migrated += 1
        print(f"  ✓ {filename} -> {doc['id']} ({doc['num_chunks']} đoạn), liên kết: {info['agents']}")
        for agent_id in info["agents"]:
            try:
                library.link(agent_id, doc["id"])
                link_count += 1
            except library.LibraryError as exc:
                print(f"    ✗ không gắn được cho agent {agent_id}: {exc}")

    print(
        f"\nXong: {migrated}/{len(by_filename)} tài liệu di chuyển, {link_count} liên kết agent<->tài liệu."
    )
    if missing_files:
        print(f"Thiếu tệp gốc cho: {', '.join(missing_files)} — liên kết các tài liệu này đã mất.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
