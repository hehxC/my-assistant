import asyncio
import hashlib
import re
import shutil
from array import array
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from bs4 import BeautifulSoup
from ebooklib import ITEM_DOCUMENT, epub
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from pypdf import PdfReader
from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..database import SessionLocal
import logging
from ..models import LibraryBook

logger = logging.getLogger(__name__)
CHUNK_SIZE = 800
CHUNK_OVERLAP = 120
EMBEDDING_BATCH_SIZE = 10
MAX_RETRIEVAL_RESULTS = 6
VECTOR_DISTANCE_THRESHOLD = 0.45
SUPPORTED_FILE_TYPES = {"pdf", "epub", "txt"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class DuplicateBookError(ValueError):
    pass


class InvalidBookError(ValueError):
    pass


class BookBusyError(ValueError):
    pass


class LibrarySource(BaseModel):
    source_id: str
    book_id: int
    title: str
    locator: str
    excerpt: str


@dataclass(frozen=True)
class ParsedSection:
    content: str
    locator: str


@dataclass(frozen=True)
class ParsedBook:
    title: str | None
    author: str | None
    sections: list[ParsedSection]


@dataclass(frozen=True)
class BookChunk:
    sequence: int
    content: str
    locator: str


@dataclass(frozen=True)
class VectorDocument:
    user_id: int
    book_id: int
    sequence: int
    title: str
    locator: str
    content: str
    vector: list[float]


class EmbeddingAdapter(Protocol):
    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class VectorIndexAdapter(Protocol):
    async def setup(self) -> None: ...

    async def upsert(self, documents: list[VectorDocument]) -> None: ...

    async def delete_book(self, book_id: int) -> None: ...

    async def count_book(self, book_id: int) -> int: ...

    async def search(
        self,
        user_id: int,
        vector: list[float],
        book_ids: list[int] | None,
        limit: int,
    ) -> list[tuple[int, str, str, str, float]]: ...


class DashScopeEmbeddingAdapter:
    """通过百炼的 OpenAI 兼容接口生成固定维度的文本向量。"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        dimensions: int,
    ):
        self.model = model
        self.dimensions = dimensions
        self.client = AsyncOpenAI(api_key=api_key or "missing", base_url=base_url or None)
        self.configured = bool(api_key and base_url)

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        if not self.configured:
            raise RuntimeError("尚未配置阿里云百炼 Embedding")

        results: list[list[float]] = []
        for offset in range(0, len(texts), EMBEDDING_BATCH_SIZE):
            batch = texts[offset : offset + EMBEDDING_BATCH_SIZE]
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    response = await self.client.embeddings.create(
                        model=self.model,
                        input=batch,
                        dimensions=self.dimensions,
                    )
                    ordered = sorted(response.data, key=lambda item: item.index)
                    vectors = [item.embedding for item in ordered]
                    if len(vectors) != len(batch):
                        raise RuntimeError("百炼返回的向量数量与输入不一致")
                    if any(len(vector) != self.dimensions for vector in vectors):
                        raise RuntimeError("百炼返回的向量维度不正确")
                    results.extend(vectors)
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt < 2:
                        await asyncio.sleep(0.5 * (2**attempt))
            if last_error is not None:
                raise RuntimeError("百炼向量服务暂时不可用") from last_error
        return results

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts)

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([text]))[0]


class RedisVectorIndex:
    """把书籍分块隐藏在独立的 Redis Search 向量索引后面。"""

    INDEX_NAME = "library_chunks_idx"
    KEY_PREFIX = "library:chunk:"

    def __init__(self, redis: Redis, dimensions: int):
        self.redis = redis
        self.dimensions = dimensions

    async def setup(self) -> None:
        try:
            await self.redis.execute_command(
                "FT.CREATE",
                self.INDEX_NAME,
                "ON",
                "HASH",
                "PREFIX",
                "1",
                self.KEY_PREFIX,
                "SCHEMA",
                "user_id",
                "TAG",
                "book_id",
                "TAG",
                "title",
                "TEXT",
                "locator",
                "TEXT",
                "content",
                "TEXT",
                "vector",
                "VECTOR",
                "HNSW",
                "6",
                "TYPE",
                "FLOAT32",
                "DIM",
                str(self.dimensions),
                "DISTANCE_METRIC",
                "COSINE",
            )
        except ResponseError as exc:
            if "Index already exists" not in str(exc):
                raise

    @staticmethod
    def _vector_bytes(vector: list[float]) -> bytes:
        return array("f", vector).tobytes()

    async def upsert(self, documents: list[VectorDocument]) -> None:
        if not documents:
            return
        async with self.redis.pipeline(transaction=False) as pipeline:
            for document in documents:
                key = f"{self.KEY_PREFIX}{document.book_id}:{document.sequence}"
                pipeline.hset(
                    key,
                    mapping={
                        "user_id": str(document.user_id),
                        "book_id": str(document.book_id),
                        "title": document.title,
                        "locator": document.locator,
                        "content": document.content,
                        "vector": self._vector_bytes(document.vector),
                    },
                )
            await pipeline.execute()

    async def delete_book(self, book_id: int) -> None:
        cursor: int | bytes = 0
        pattern = f"{self.KEY_PREFIX}{book_id}:*"
        while True:
            cursor, keys = await self.redis.scan(cursor=cursor, match=pattern, count=500)
            if keys:
                await self.redis.unlink(*keys)
            if int(cursor) == 0:
                break

    async def count_book(self, book_id: int) -> int:
        try:
            result = await self.redis.execute_command(
                "FT.SEARCH",
                self.INDEX_NAME,
                f"@book_id:{{{book_id}}}",
                "LIMIT",
                "0",
                "0",
                "DIALECT",
                "2",
            )
            if isinstance(result, dict):
                return int(result.get(b"total_results", result.get("total_results", 0)))
            return int(result[0])
        except ResponseError:
            return 0

    async def search(
        self,
        user_id: int,
        vector: list[float],
        book_ids: list[int] | None,
        limit: int,
    ) -> list[tuple[int, str, str, str, float]]:
        filters = [f"@user_id:{{{user_id}}}"]
        if book_ids:
            filters.append("@book_id:{" + "|".join(map(str, book_ids)) + "}")
        query = f"({' '.join(filters)})=>[KNN {limit} @vector $query_vector AS distance]"
        result = await self.redis.execute_command(
            "FT.SEARCH",
            self.INDEX_NAME,
            query,
            "PARAMS",
            "2",
            "query_vector",
            self._vector_bytes(vector),
            "SORTBY",
            "distance",
            "RETURN",
            "5",
            "book_id",
            "title",
            "locator",
            "content",
            "distance",
            "LIMIT",
            "0",
            str(limit),
            "DIALECT",
            "2",
        )
        rows = []
        if isinstance(result, dict):
            raw_rows = [
                item.get(b"extra_attributes", item.get("extra_attributes", {}))
                for item in result.get(b"results", result.get("results", []))
            ]
        else:
            raw_rows = [result[index + 1] for index in range(1, len(result), 2)]
        for raw_fields in raw_rows:
            if isinstance(raw_fields, dict):
                fields = {
                    self._decode(key): self._decode(value)
                    for key, value in raw_fields.items()
                }
            else:
                fields = {
                    self._decode(raw_fields[position]): self._decode(raw_fields[position + 1])
                    for position in range(0, len(raw_fields), 2)
                }
            rows.append(
                (
                    int(fields["book_id"]),
                    fields["title"],
                    fields["locator"],
                    fields["content"],
                    float(fields["distance"]),
                )
            )
        return rows

    @staticmethod
    def _decode(value: bytes | str) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _clean_text(value: str) -> str:
    value = value.replace("\u00a0", " ").replace("\x00", "")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def parse_book(path: Path, file_type: str) -> ParsedBook:
    if file_type == "pdf":
        reader = PdfReader(path)
        metadata = reader.metadata
        sections = []
        for page_number, page in enumerate(reader.pages, start=1):
            text = _clean_text(page.extract_text() or "")
            if text:
                sections.append(ParsedSection(text, f"第 {page_number} 页"))
        return ParsedBook(
            title=_clean_text(str(metadata.title)) if metadata and metadata.title else None,
            author=_clean_text(str(metadata.author)) if metadata and metadata.author else None,
            sections=sections,
        )

    if file_type == "epub":
        book = epub.read_epub(str(path), options={"ignore_ncx": True})
        title_values = book.get_metadata("DC", "title")
        author_values = book.get_metadata("DC", "creator")
        sections = []
        for item in book.get_items_of_type(ITEM_DOCUMENT):
            soup = BeautifulSoup(item.get_content(), "html.parser")
            text = _clean_text(soup.get_text("\n"))
            if not text:
                continue
            heading = soup.find(["h1", "h2", "h3"])
            locator = _clean_text(heading.get_text(" ")) if heading else item.get_name()
            sections.append(ParsedSection(text, locator[:255] or "未命名章节"))
        return ParsedBook(
            title=_clean_text(str(title_values[0][0])) if title_values else None,
            author=_clean_text(str(author_values[0][0])) if author_values else None,
            sections=sections,
        )

    if file_type == "txt":
        text = path.read_text(encoding="utf-8-sig")
        lines = text.splitlines()
        sections = []
        for offset in range(0, len(lines), 80):
            block = _clean_text("\n".join(lines[offset : offset + 80]))
            if block:
                end = min(offset + 80, len(lines))
                sections.append(ParsedSection(block, f"第 {offset + 1}–{end} 行"))
        return ParsedBook(title=None, author=None, sections=sections)

    raise InvalidBookError("不支持这种电子书格式")


def split_sections(sections: list[ParsedSection]) -> list[BookChunk]:
    chunks = []
    sequence = 0
    step = CHUNK_SIZE - CHUNK_OVERLAP
    for section in sections:
        for offset in range(0, len(section.content), step):
            content = section.content[offset : offset + CHUNK_SIZE].strip()
            if not content:
                continue
            chunks.append(BookChunk(sequence, content, section.locator))
            sequence += 1
            if offset + CHUNK_SIZE >= len(section.content):
                break
    return chunks


class LibraryService:
    """封装书籍生命周期和 RAG 检索规则的深模块。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session] = SessionLocal,
        storage_root: str | Path = "data/library",
        max_upload_bytes: int = 50 * 1024 * 1024,
        embedding: EmbeddingAdapter | None = None,
        vector_index: VectorIndexAdapter | None = None,
        embedding_model: str = "text-embedding-v4",
        embedding_dimension: int = 1024,
    ):
        self.session_factory = session_factory
        self.storage_root = Path(storage_root).resolve()
        self.max_upload_bytes = max_upload_bytes
        self.embedding = embedding
        self.vector_index = vector_index
        self.embedding_model = embedding_model
        self.embedding_dimension = embedding_dimension

    async def setup(self) -> None:
        self.storage_root.mkdir(parents=True, exist_ok=True)
        if self.vector_index is not None:
            await self.vector_index.setup()

    async def create_upload(self, user_id: int, filename: str, content: bytes) -> LibraryBook:
        file_type = Path(filename).suffix.lower().lstrip(".")
        if file_type not in SUPPORTED_FILE_TYPES:
            raise InvalidBookError("仅支持 PDF、EPUB 和 TXT 文件")
        if not content:
            raise InvalidBookError("不能上传空文件")
        if len(content) > self.max_upload_bytes:
            raise InvalidBookError("文件大小超过限制")
        if file_type == "pdf" and not content.startswith(b"%PDF"):
            raise InvalidBookError("文件内容不是有效的 PDF")
        if file_type == "epub" and not content.startswith(b"PK"):
            raise InvalidBookError("文件内容不是有效的 EPUB")
        if file_type == "txt" and b"\x00" in content[:4096]:
            raise InvalidBookError("TXT 文件编码或内容无效")

        digest = hashlib.sha256(content).hexdigest()
        safe_filename = Path(filename).name[:255]
        title = Path(safe_filename).stem.strip()[:255] or "未命名书籍"
        now = utc_now()

        def save() -> LibraryBook:
            with self.session_factory() as db:
                existing = db.scalar(
                    select(LibraryBook).where(
                        LibraryBook.user_id == user_id,
                        LibraryBook.sha256 == digest,
                    )
                )
                if existing is not None:
                    raise DuplicateBookError("这本书已经上传过了")
                book = LibraryBook(
                    user_id=user_id,
                    title=title,
                    author=None,
                    original_filename=safe_filename,
                    file_type=file_type,
                    file_size=len(content),
                    sha256=digest,
                    storage_path="",
                    status="queued",
                    processed_chunks=0,
                    chunk_count=0,
                    embedding_model=self.embedding_model,
                    embedding_dimension=self.embedding_dimension,
                    error_message=None,
                    created_at=now,
                    updated_at=now,
                )
                try:
                    db.add(book)
                    db.flush()
                    relative_path = Path(str(user_id)) / str(book.id) / f"source.{file_type}"
                    absolute_path = self.storage_root / relative_path
                    absolute_path.parent.mkdir(parents=True, exist_ok=True)
                    absolute_path.write_bytes(content)
                    book.storage_path = relative_path.as_posix()
                    db.commit()
                    db.refresh(book)
                    return book
                except IntegrityError as exc:
                    db.rollback()
                    raise DuplicateBookError("这本书已经上传过了") from exc
                except Exception:
                    db.rollback()
                    if "absolute_path" in locals():
                        shutil.rmtree(absolute_path.parent, ignore_errors=True)
                    raise

        return await asyncio.to_thread(save)

    async def list_books(self, user_id: int) -> list[LibraryBook]:
        def load() -> list[LibraryBook]:
            with self.session_factory() as db:
                return list(
                    db.scalars(
                        select(LibraryBook)
                        .where(LibraryBook.user_id == user_id)
                        .order_by(LibraryBook.created_at.desc(), LibraryBook.id.desc())
                    ).all()
                )

        return await asyncio.to_thread(load)

    async def get_book(self, user_id: int, book_id: int) -> LibraryBook | None:
        def load() -> LibraryBook | None:
            with self.session_factory() as db:
                return db.scalar(
                    select(LibraryBook).where(
                        LibraryBook.id == book_id,
                        LibraryBook.user_id == user_id,
                    )
                )

        return await asyncio.to_thread(load)

    async def validate_ready_books(self, user_id: int, book_ids: list[int]) -> None:
        unique_ids = set(book_ids)
        if not unique_ids:
            raise InvalidBookError("请至少选择一本已入库的书")

        def count() -> int:
            with self.session_factory() as db:
                return len(
                    db.scalars(
                        select(LibraryBook.id).where(
                            LibraryBook.user_id == user_id,
                            LibraryBook.id.in_(unique_ids),
                            LibraryBook.status == "ready",
                        )
                    ).all()
                )

        if await asyncio.to_thread(count) != len(unique_ids):
            raise InvalidBookError("选择的书籍不存在或尚未处理完成")

    async def retry(self, user_id: int, book_id: int) -> LibraryBook | None:
        def update() -> LibraryBook | None:
            with self.session_factory() as db:
                book = db.scalar(
                    select(LibraryBook).where(
                        LibraryBook.id == book_id,
                        LibraryBook.user_id == user_id,
                    )
                )
                if book is None:
                    return None
                if book.status == "processing":
                    raise BookBusyError("这本书正在处理")
                if book.status != "failed":
                    raise BookBusyError("只有处理失败的书籍可以手动重试")
                book.status = "queued"
                book.processed_chunks = 0
                book.error_message = None
                book.updated_at = utc_now()
                db.commit()
                db.refresh(book)
                return book

        return await asyncio.to_thread(update)

    async def delete(self, user_id: int, book_id: int) -> bool:
        book = await self.get_book(user_id, book_id)
        if book is None:
            return False
        if book.status == "processing":
            raise BookBusyError("正在处理的书暂时不能删除")
        if self.vector_index is not None:
            await self.vector_index.delete_book(book_id)

        absolute_path = self._absolute_storage_path(book.storage_path)
        await asyncio.to_thread(shutil.rmtree, absolute_path.parent, True)

        def remove() -> None:
            with self.session_factory.begin() as db:
                owned = db.scalar(
                    select(LibraryBook).where(
                        LibraryBook.id == book_id,
                        LibraryBook.user_id == user_id,
                    )
                )
                if owned is not None:
                    db.delete(owned)

        await asyncio.to_thread(remove)
        return True

    async def process_book(self, book_id: int) -> None:
        if self.embedding is None or self.vector_index is None:
            await self._mark_failed(book_id, "书库向量服务尚未配置")
            return

        book = await self._set_processing(book_id)
        if book is None:
            return
        try:
            path = self._absolute_storage_path(book.storage_path)
            parsed = await asyncio.to_thread(parse_book, path, book.file_type)
            chunks = split_sections(parsed.sections)
            if not chunks:
                raise InvalidBookError("电子书中没有可提取的文字")

            title = (parsed.title or book.title).strip()[:255]
            author = parsed.author.strip()[:255] if parsed.author else None
            await self._set_chunk_total(book.id, title, author, len(chunks))
            await self.vector_index.delete_book(book.id)

            processed = 0
            for offset in range(0, len(chunks), EMBEDDING_BATCH_SIZE):
                batch = chunks[offset : offset + EMBEDDING_BATCH_SIZE]
                vectors = await self.embedding.embed_documents(
                    [chunk.content for chunk in batch]
                )
                documents = [
                    VectorDocument(
                        user_id=book.user_id,
                        book_id=book.id,
                        sequence=chunk.sequence,
                        title=title,
                        locator=chunk.locator,
                        content=chunk.content,
                        vector=vector,
                    )
                    for chunk, vector in zip(batch, vectors, strict=True)
                ]
                await self.vector_index.upsert(documents)
                processed += len(batch)
                await self._set_progress(book.id, processed)
            await self._mark_ready(book.id)
        except Exception as exc:
            await self.vector_index.delete_book(book.id)
            await self._mark_failed(book.id, str(exc)[:1000] or "处理电子书失败")

    async def retrieve(
        self,
        user_id: int,
        query: str,
        book_ids: list[int] | None = None,
    ) -> list[LibrarySource]:
        if self.embedding is None or self.vector_index is None or not query.strip():
            return []
        if book_ids:
            await self.validate_ready_books(user_id, book_ids)
        elif not await self._has_ready_books(user_id):
            return []

        query_vector = await self.embedding.embed_query(query[:8000])
        rows = await self.vector_index.search(user_id, query_vector, book_ids, 50)
        for book_id, title, locator, content, distance in rows:
            logger.info(
                "RAG candidate book_id=%s title=%r locator=%r "
                "distance=%.4f content=%r",
                book_id,
                title,
                locator,
                distance,
                content,
            )
        sources = []
        for book_id, title, locator, content, distance in rows:
            if distance > VECTOR_DISTANCE_THRESHOLD:
                continue
            source_id = f"S{len(sources) + 1}"
            sources.append(
                LibrarySource(
                    source_id=source_id,
                    book_id=book_id,
                    title=title,
                    locator=locator,
                    excerpt=content[:500],
                )
            )
            if len(sources) >= MAX_RETRIEVAL_RESULTS:
                break
        return sources

    async def books_needing_recovery(self) -> list[int]:
        def load() -> tuple[list[int], list[tuple[int, int]]]:
            with self.session_factory() as db:
                queued = list(
                    db.scalars(
                        select(LibraryBook.id).where(
                            LibraryBook.status.in_(["queued", "processing"])
                        )
                    ).all()
                )
                ready = list(
                    db.execute(
                        select(LibraryBook.id, LibraryBook.chunk_count).where(
                            LibraryBook.status == "ready"
                        )
                    ).all()
                )
                return queued, ready

        queued, ready = await asyncio.to_thread(load)
        if self.vector_index is None:
            return queued
        missing = []
        for book_id, chunk_count in ready:
            if await self.vector_index.count_book(book_id) != chunk_count:
                missing.append(book_id)
        for book_id in missing:
            await self._set_queued(book_id)
        return [*queued, *missing]

    def _absolute_storage_path(self, relative_path: str) -> Path:
        path = (self.storage_root / relative_path).resolve()
        if self.storage_root not in path.parents:
            raise InvalidBookError("书籍存储路径无效")
        return path

    async def _has_ready_books(self, user_id: int) -> bool:
        def exists() -> bool:
            with self.session_factory() as db:
                return (
                    db.scalar(
                        select(LibraryBook.id)
                        .where(
                            LibraryBook.user_id == user_id,
                            LibraryBook.status == "ready",
                        )
                        .limit(1)
                    )
                    is not None
                )

        return await asyncio.to_thread(exists)

    async def _set_processing(self, book_id: int) -> LibraryBook | None:
        def update() -> LibraryBook | None:
            with self.session_factory() as db:
                book = db.get(LibraryBook, book_id)
                if book is None or book.status not in {"queued", "processing"}:
                    return None
                book.status = "processing"
                book.processed_chunks = 0
                book.error_message = None
                book.updated_at = utc_now()
                db.commit()
                db.refresh(book)
                return book

        return await asyncio.to_thread(update)

    async def _set_chunk_total(
        self, book_id: int, title: str, author: str | None, chunk_count: int
    ) -> None:
        def update() -> None:
            with self.session_factory.begin() as db:
                book = db.get(LibraryBook, book_id)
                if book is not None:
                    book.title = title
                    book.author = author
                    book.chunk_count = chunk_count
                    book.processed_chunks = 0
                    book.updated_at = utc_now()

        await asyncio.to_thread(update)

    async def _set_progress(self, book_id: int, processed: int) -> None:
        def update() -> None:
            with self.session_factory.begin() as db:
                book = db.get(LibraryBook, book_id)
                if book is not None:
                    book.processed_chunks = processed
                    book.updated_at = utc_now()

        await asyncio.to_thread(update)

    async def _mark_ready(self, book_id: int) -> None:
        def update() -> None:
            with self.session_factory.begin() as db:
                book = db.get(LibraryBook, book_id)
                if book is not None:
                    book.status = "ready"
                    book.processed_chunks = book.chunk_count
                    book.error_message = None
                    book.updated_at = utc_now()

        await asyncio.to_thread(update)

    async def _mark_failed(self, book_id: int, error: str) -> None:
        def update() -> None:
            with self.session_factory.begin() as db:
                book = db.get(LibraryBook, book_id)
                if book is not None:
                    book.status = "failed"
                    book.processed_chunks = 0
                    book.error_message = error
                    book.updated_at = utc_now()

        await asyncio.to_thread(update)

    async def _set_queued(self, book_id: int) -> None:
        def update() -> None:
            with self.session_factory.begin() as db:
                book = db.get(LibraryBook, book_id)
                if book is not None:
                    book.status = "queued"
                    book.processed_chunks = 0
                    book.updated_at = utc_now()

        await asyncio.to_thread(update)


class LibraryIngestionWorker:
    """用单消费者队列串行处理电子书，避免并发压垮外部向量服务。"""

    def __init__(self, library: LibraryService):
        self.library = library
        self.queue: asyncio.Queue[int | None] = asyncio.Queue()
        self.queued_ids: set[int] = set()
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        await self.library.setup()
        self.task = asyncio.create_task(self._run(), name="library-ingestion-worker")
        for book_id in await self.library.books_needing_recovery():
            await self.enqueue(book_id)

    async def stop(self) -> None:
        if self.task is None:
            return
        await self.queue.put(None)
        await self.task
        self.task = None

    async def enqueue(self, book_id: int) -> None:
        if book_id in self.queued_ids:
            return
        self.queued_ids.add(book_id)
        await self.queue.put(book_id)

    async def _run(self) -> None:
        while True:
            book_id = await self.queue.get()
            if book_id is None:
                self.queue.task_done()
                return
            try:
                await self.library.process_book(book_id)
            finally:
                self.queued_ids.discard(book_id)
                self.queue.task_done()
