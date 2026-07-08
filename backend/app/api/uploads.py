"""Shared upload size-cap helpers for the /imports and /hub upload routes.

The Content-Length header is checked first as a fast pre-check (the multipart
body is always at least as large as the file), but the header cannot be
trusted: the capped chunked read is the authoritative limit on the bytes
actually received.
"""

from fastapi import HTTPException, Request, UploadFile

_READ_CHUNK_BYTES = 1024 * 1024


def upload_too_large(max_upload_bytes: int, noun: str = "file") -> HTTPException:
    return HTTPException(
        status_code=413,
        detail=f"Uploaded {noun} exceeds the maximum allowed size of {max_upload_bytes} bytes.",
    )


def check_content_length(request: Request, max_upload_bytes: int, noun: str = "file") -> None:
    """Fast pre-check on the declared body size; raises 413 when it exceeds the cap."""
    content_length = request.headers.get("content-length")
    if content_length is not None and content_length.isdigit() and int(content_length) > max_upload_bytes:
        raise upload_too_large(max_upload_bytes, noun)


async def read_upload_capped(file: UploadFile, max_upload_bytes: int, noun: str = "file") -> bytes:
    """Read the upload in chunks, rejecting once the cap is exceeded."""
    buffer = bytearray()
    while chunk := await file.read(_READ_CHUNK_BYTES):
        buffer.extend(chunk)
        if len(buffer) > max_upload_bytes:
            raise upload_too_large(max_upload_bytes, noun)
    return bytes(buffer)
