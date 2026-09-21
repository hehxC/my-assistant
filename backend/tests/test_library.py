import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from ebooklib import epub
from pypdf import PdfWriter
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.database import Base
from backend.app.models import LibraryBook, User
from backend.app.services.library import (
    BookBusyError,
    DashScopeEmbeddingAdapter,
    DuplicateBookError,
    InvalidBookError,
    LibraryIngestionWorker,
    LibraryService,
    RedisVectorIndex,
    VectorDocument,
    parse_book,
    split_sections,
)


class FakeEmbedding:
    def __init__(self):
        self.document_batches = []
        self.queries = []

    async def embed_documents(self, texts):
        self.document_batches.append(texts)
        return [[float(len(text)), 0.0, 1.0] for text in texts]

    async def embed_query(self, text):
        self.queries.append(text)
        return [float(len(text)), 0.0, 1.0]


class FakeVectorIndex:
    def __init__(self):
        self.documents: dict[tuple[int, int], VectorDocument] = {}
        self.setup_calls = 0

    async def setup(self):
        self.setup_calls += 1

    async def upsert(self, documents):
        for document in documents:
            self.documents[(document.book_id, document.sequence)] = document

    async def delete_book(self, book_id):
        self.documents = {
            key: value for key, value in self.documents.items() if value.book_id != book_id
        }

    async def count_book(self, book_id):
        return sum(document.book_id == book_id for document in self.documents.values())

    async def search(self, user_id, _vector, book_ids, limit):
        rows = []
        for document in self.documents.values():
            if document.user_id != user_id:
                continue
            if book_ids and document.book_id not in book_ids:
                continue
            rows.append(
                (
                    document.book_id,
                    document.title,
                    document.locator,
                    document.content,
                    0.1,
                )
            )
        return rows[:limit]


@pytest.fixture
def library_context(tmp_path):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)
    with testing_session.begin() as db:
        db.add_all(
            [
                User(
                    id=1,
                    username="alice",
                    username_normalized="alice",
                    password_hash="hash",
                    created_at=datetime(2026, 9, 16),
                ),
                User(
                    id=2,
                    username="bobby",
                    username_normalized="bobby",
                    password_hash="hash",
                    created_at=datetime(2026, 9, 16),
                ),
            ]
        )
    embedding = FakeEmbedding()
    vector_index = FakeVectorIndex()
    library = LibraryService(
        session_factory=testing_session,
        storage_root=tmp_path / "library",
        max_upload_bytes=1024 * 1024,
        embedding=embedding,
        vector_index=vector_index,
        embedding_model="fake-embedding",
        embedding_dimension=3,
    )
    yield library, testing_session, embedding, vector_index
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_library_table_and_columns_have_database_comments():
    table = LibraryBook.__table__

    assert table.comment
    assert all(column.comment for column in table.columns)


def test_txt_upload_process_retrieve_and_delete_are_user_scoped(library_context):
    library, _, embedding, vector_index = library_context
    content = ("向量检索可以帮助助手从书籍中找到相关内容。\n" * 80).encode()

    async def scenario():
        await library.setup()
        book = await library.create_upload(1, "RAG 入门.txt", content)
        await library.process_book(book.id)
        ready = await library.get_book(1, book.id)
        sources = await library.retrieve(1, "什么是向量检索？")
        other_sources = await library.retrieve(2, "什么是向量检索？")
        deleted = await library.delete(1, book.id)
        return book, ready, sources, other_sources, deleted

    book, ready, sources, other_sources, deleted = asyncio.run(scenario())

    assert ready.status == "ready"
    assert ready.chunk_count > 1
    assert ready.processed_chunks == ready.chunk_count
    assert embedding.document_batches
    assert sources[0].book_id == book.id
    assert sources[0].locator.startswith("第 ")
    assert other_sources == []
    assert deleted is True
    assert vector_index.documents == {}


def test_duplicate_invalid_and_empty_uploads_are_rejected(library_context):
    library, _, _, _ = library_context

    async def scenario():
        await library.create_upload(1, "book.txt", "有效内容".encode())
        with pytest.raises(DuplicateBookError):
            await library.create_upload(1, "copy.txt", "有效内容".encode())
        with pytest.raises(InvalidBookError):
            await library.create_upload(1, "book.docx", b"content")
        with pytest.raises(InvalidBookError):
            await library.create_upload(1, "empty.txt", b"")

    asyncio.run(scenario())


def test_epub_parser_preserves_chapter_and_blank_pdf_has_no_text(tmp_path):
    epub_path = tmp_path / "book.epub"
    book = epub.EpubBook()
    book.set_identifier("test-book")
    book.set_title("测试电子书")
    book.add_author("测试作者")
    chapter = epub.EpubHtml(title="第一章", file_name="chapter.xhtml", lang="zh")
    chapter.content = "<h1>第一章 起点</h1><p>这是章节正文。</p>"
    book.add_item(chapter)
    book.spine = ["nav", chapter]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(str(epub_path), book)

    parsed_epub = parse_book(epub_path, "epub")
    chunks = split_sections(parsed_epub.sections)

    pdf_path = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with pdf_path.open("wb") as stream:
        writer.write(stream)
    parsed_pdf = parse_book(pdf_path, "pdf")

    assert parsed_epub.title == "测试电子书"
    assert parsed_epub.author == "测试作者"
    assert chunks[0].locator == "第一章 起点"
    assert "章节正文" in chunks[0].content
    assert parsed_pdf.sections == []


def test_worker_recovers_queued_books(library_context):
    library, _, _, vector_index = library_context

    async def scenario():
        book = await library.create_upload(1, "queued.txt", "恢复任务内容".encode())
        worker = LibraryIngestionWorker(library)
        await worker.start()
        await asyncio.wait_for(worker.queue.join(), timeout=2)
        processed = await library.get_book(1, book.id)
        await worker.stop()
        return processed

    processed = asyncio.run(scenario())

    assert vector_index.setup_calls == 1
    assert processed.status == "ready"


def test_failed_ingestion_removes_partial_vectors(library_context):
    library, _, _, vector_index = library_context

    class FailingEmbedding(FakeEmbedding):
        async def embed_documents(self, texts):
            if self.document_batches:
                raise RuntimeError("provider unavailable")
            return await super().embed_documents(texts)

    library.embedding = FailingEmbedding()
    content = ("一个足够长的段落用于生成许多文本分块。" * 600).encode()

    async def scenario():
        book = await library.create_upload(1, "long.txt", content)
        await library.process_book(book.id)
        return await library.get_book(1, book.id)

    failed = asyncio.run(scenario())

    assert failed.status == "failed"
    assert vector_index.documents == {}


def test_retry_only_accepts_failed_books(library_context):
    library, _, _, _ = library_context

    async def scenario():
        book = await library.create_upload(1, "retry.txt", "等待处理".encode())
        with pytest.raises(BookBusyError, match="只有处理失败"):
            await library.retry(1, book.id)

    asyncio.run(scenario())


def test_missing_ready_vectors_are_requeued(library_context):
    library, _, _, vector_index = library_context

    async def scenario():
        book = await library.create_upload(1, "recover.txt", "需要恢复的内容".encode())
        await library.process_book(book.id)
        vector_index.documents.clear()
        recovery_ids = await library.books_needing_recovery()
        recovered = await library.get_book(1, book.id)
        return book.id, recovery_ids, recovered

    book_id, recovery_ids, recovered = asyncio.run(scenario())

    assert recovery_ids == [book_id]
    assert recovered.status == "queued"


def test_dashscope_adapter_batches_at_ten_and_validates_dimensions():
    calls = []

    class FakeEmbeddingsClient:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                data=[
                    SimpleNamespace(index=index, embedding=[0.0] * 4)
                    for index in range(len(kwargs["input"]))
                ]
            )

    adapter = DashScopeEmbeddingAdapter("key", "https://example.com/v1", "model", 4)
    adapter.client = SimpleNamespace(embeddings=FakeEmbeddingsClient())

    vectors = asyncio.run(adapter.embed_documents([f"text-{index}" for index in range(23)]))

    assert len(vectors) == 23
    assert [len(call["input"]) for call in calls] == [10, 10, 3]
    assert all(call["dimensions"] == 4 for call in calls)


def test_redis_vector_index_parses_resp3_search_results():
    class FakeRedis:
        async def execute_command(self, *args):
            if "KNN" in str(args[2]):
                return {
                    b"results": [
                        {
                            b"extra_attributes": {
                                b"book_id": b"12",
                                b"title": "测试书".encode(),
                                b"locator": "第 2 页".encode(),
                                b"content": "命中内容".encode(),
                                b"distance": b"0.12",
                            }
                        }
                    ],
                    b"total_results": 1,
                }
            return {b"total_results": 3}

    index = RedisVectorIndex(FakeRedis(), dimensions=4)

    async def scenario():
        count = await index.count_book(12)
        rows = await index.search(1, [1.0, 0.0, 0.0, 0.0], [12], 3)
        return count, rows

    count, rows = asyncio.run(scenario())

    assert count == 3
    assert rows == [(12, "测试书", "第 2 页", "命中内容", 0.12)]
