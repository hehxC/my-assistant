from datetime import datetime
from typing import Literal

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict

from ..services.library import BookBusyError, DuplicateBookError, InvalidBookError
from .auth import CurrentUser


router = APIRouter(prefix="/api/library", tags=["library"])


class LibraryBookResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    author: str | None
    original_filename: str
    file_type: str
    file_size: int
    status: Literal["queued", "processing", "ready", "failed"]
    processed_chunks: int
    chunk_count: int
    error_message: str | None
    created_at: datetime
    updated_at: datetime


def _library(request: Request):
    return request.app.state.library_service


def _worker(request: Request):
    return request.app.state.library_worker


@router.post("", response_model=LibraryBookResponse, status_code=202)
async def upload_book(
    request: Request,
    current_user: CurrentUser,
    file: UploadFile = File(...),
):
    filename = file.filename or ""
    max_bytes = _library(request).max_upload_bytes
    content = await file.read(max_bytes + 1)
    await file.close()
    try:
        book = await _library(request).create_upload(current_user.id, filename, content)
    except DuplicateBookError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidBookError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="暂时无法保存电子书") from exc
    # 保存完文件后用队列完成文件写入向量库
    await _worker(request).enqueue(book.id)
    return book


@router.get("", response_model=list[LibraryBookResponse])
async def list_books(request: Request, current_user: CurrentUser):
    return await _library(request).list_books(current_user.id)


@router.get("/{book_id}", response_model=LibraryBookResponse)
async def get_book(book_id: int, request: Request, current_user: CurrentUser):
    book = await _library(request).get_book(current_user.id, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="没有找到这本书")
    return book


@router.post("/{book_id}/retry", response_model=LibraryBookResponse, status_code=202)
async def retry_book(book_id: int, request: Request, current_user: CurrentUser):
    try:
        book = await _library(request).retry(current_user.id, book_id)
    except BookBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if book is None:
        raise HTTPException(status_code=404, detail="没有找到这本书")
    await _worker(request).enqueue(book.id)
    return book


@router.delete("/{book_id}", status_code=204)
async def delete_book(
    book_id: int,
    request: Request,
    current_user: CurrentUser,
) -> Response:
    try:
        deleted = await _library(request).delete(current_user.id, book_id)
    except BookBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="没有找到这本书")
    return Response(status_code=204)
