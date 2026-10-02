"""Minimal multipart/related parsing and rendering helpers."""
from __future__ import annotations

import re


class MultipartError(Exception):
    pass


def parse_boundary(content_type: str) -> bytes:
    match = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if not match:
        raise MultipartError("missing multipart boundary")
    return match.group(1).encode()


def parse_parts(body: bytes, boundary: bytes):
    """Yield (headers dict, payload bytes) for each part."""
    delimiter = b"--" + boundary
    segments = body.split(delimiter)
    for segment in segments[1:]:
        if segment.startswith(b"--"):
            break
        segment = segment.lstrip(b"\r\n")
        if not segment:
            continue
        head, sep, payload = segment.partition(b"\r\n\r\n")
        if not sep:
            raise MultipartError("malformed multipart segment")
        headers = {}
        for line in head.split(b"\r\n"):
            name, _, value = line.partition(b":")
            headers[name.strip().lower().decode()] = value.strip().decode()
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        yield headers, payload


def render(parts, boundary: str, content_type: str) -> bytes:
    out = bytearray()
    for payload in parts:
        out += f"--{boundary}\r\nContent-Type: {content_type}\r\n\r\n".encode()
        out += payload
        out += b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out)
