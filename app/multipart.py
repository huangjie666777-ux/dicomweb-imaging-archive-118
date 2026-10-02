"""Streaming multipart/related parser for STOW-RS requests.

Only the subset required by DICOM PS3.18 Store transaction is supported:
CRLF delimiters and exactly one part per DICOM Part 10 instance.
Part bodies are spooled to temporary files so request size is not bounded by RAM.
"""

from __future__ import annotations

from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default as default_policy
from pathlib import Path
import tempfile
from typing import AsyncIterator

from starlette.requests import Request


DICOM_MEDIA_TYPE = "application/dicom"


class MultipartError(Exception):
    pass


class PayloadTooLarge(MultipartError):
    pass


class InstanceLimitExceeded(MultipartError):
    pass


@dataclass
class Part:
    path: Path
    size: int
    content_type: str | None

    def cleanup(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def extract_boundary(content_type: str | None) -> bytes:
    if not content_type:
        raise MultipartError("missing Content-Type header")
    # email parser tolerates parameters and quoted values.
    msg = BytesParser(policy=default_policy).parsebytes(
        f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
    )
    maintype = msg.get_content_type()
    if maintype != "multipart/related":
        raise MultipartError("Content-Type must be multipart/related")
    boundary = msg.get_boundary()
    if not boundary:
        raise MultipartError("multipart/related Content-Type lacks a boundary")
    return boundary.encode("ascii")


async def iter_parts(
    request: Request,
    boundary: bytes,
    tmp_dir: Path,
    max_bytes: int,
    max_instances: int,
) -> AsyncIterator[Part]:
    delimiter = b"--" + boundary
    tmp_dir.mkdir(parents=True, exist_ok=True)

    state = "preamble"
    buf = b""
    out = None
    out_path: Path | None = None
    part_size = 0
    part_count = 0
    total_bytes = 0
    headers = b""
    content_type: str | None = None

    async def finish_part() -> Part | None:
        nonlocal out, out_path, part_size, content_type
        if out is None or out_path is None:
            return None
        out.close()
        # Strip the trailing CRLF belonging to the boundary delimiter.
        if part_size >= 2:
            with out_path.open("rb+") as fix:
                fix.seek(-2, 2)
                tail = fix.read(2)
                if tail == b"\r\n":
                    fix.seek(-2, 2)
                    fix.truncate()
                    part_size -= 2
        parsed = BytesParser(policy=default_policy).parsebytes(headers + b"\r\n")
        ct = parsed.get("Content-Type", content_type)
        part = Part(path=out_path, size=part_size, content_type=ct)
        out = None
        out_path = None
        part_size_local = part_size
        part_size = 0
        return part

    def open_part(header_block: bytes) -> None:
        nonlocal out, out_path, part_size, headers, content_type
        headers = header_block
        parsed = BytesParser(policy=default_policy).parsebytes(headers + b"\r\n")
        content_type = parsed.get("Content-Type")
        fd, name = tempfile.mkstemp(prefix="part-", suffix=".part", dir=tmp_dir)
        out_path = Path(name)
        out = open(fd, "wb")
        part_size = 0

    def write_body(data: bytes) -> None:
        nonlocal part_size
        if not data:
            return
        assert out is not None
        out.write(data)
        out.flush()
        part_size += len(data)

    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            buf += chunk
            total_bytes += len(chunk)
            if total_bytes > max_bytes:
                raise PayloadTooLarge(f"request body exceeds {max_bytes} bytes")
            while True:
                if state == "preamble":
                    marker = buf.find(delimiter)
                    if marker < 0:
                        keep = len(delimiter) + 3
                        if len(buf) > keep:
                            buf = buf[-keep:]
                        break
                    after = marker + len(delimiter)
                    suffix = buf[after:after + 2]
                    if suffix == b"--":
                        state = "done"
                        buf = b""
                        break
                    if suffix != b"\r\n":
                        raise MultipartError("malformed multipart boundary")
                    state = "headers"
                    buf = buf[after + 2 :]
                    headers = b""
                elif state == "headers":
                    end = buf.find(b"\r\n\r\n")
                    if end < 0:
                        if len(buf) > 64 * 1024:
                            raise MultipartError("part headers too large")
                        break
                    header_block = buf[:end].replace(b"\r\n", b"\n")
                    open_part(header_block)
                    part_count += 1
                    if part_count > max_instances:
                        raise InstanceLimitExceeded(
                            f"request contains more than {max_instances} instances"
                        )
                    state = "body"
                    buf = buf[end + 4 :]
                elif state == "body":
                    marker = buf.find(b"\r\n" + delimiter)
                    if marker < 0:
                        safe = max(0, len(buf) - (len(delimiter) + 4))
                        write_body(buf[:safe])
                        buf = buf[safe:]
                        break
                    write_body(buf[: marker + 2])
                    finished = await finish_part()
                    if finished is not None:
                        yield finished
                    after = marker + 2 + len(delimiter)
                    suffix = buf[after : after + 2]
                    if suffix == b"--":
                        state = "done"
                        buf = b""
                        break
                    if suffix != b"\r\n":
                        raise MultipartError("malformed multipart boundary")
                    state = "headers"
                    buf = buf[after + 2 :]
                    headers = b""
                elif state == "done":
                    break
            if state == "done":
                break
        else:  # pragma: no cover - async iterator always terminates normally
            pass
        if state != "done":
            raise MultipartError("incomplete multipart/related request")
    except Exception:
        if out is not None:
            out.close()
        if out_path is not None:
            try:
                out_path.unlink()
            except FileNotFoundError:
                pass
        raise

    epilogue = buf.strip()
    if epilogue and not epilogue.startswith(b"--"):
        # Trailing CR/LF after the closing boundary is allowed; other data is ignored
        # conservatively only when it consists solely of whitespace.
        if epilogue.strip(b"\r\n\t "):
            raise MultipartError("unexpected data after closing boundary")
