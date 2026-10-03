"""Bounded image downloads for server-generated provider results."""

from __future__ import annotations

import base64
import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urljoin, urlsplit


MAX_IMAGE_BYTES = 30 * 1024 * 1024
IMAGE_CONTENT_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    """Pin the validated address while retaining hostname verification and SNI."""

    def __init__(self, hostname: str, address: str):
        super().__init__(hostname, timeout=30)
        self.address = address

    def connect(self) -> None:
        sock = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def _download_image(url: str) -> tuple[bytes, str]:
    # These URLs come only from the backend provider, never tool arguments.
    # Validate every redirect and pin DNS to prevent access to internal services.
    for _ in range(4):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https" or not parsed.hostname or parsed.port not in {None, 443}
            or parsed.username is not None or parsed.password is not None
            or any(ord(char) < 33 or ord(char) == 127 for char in url)
        ):
            raise ValueError("unsupported image URL")
        addresses = [item[4][0] for item in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)]
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError("image URL must resolve to public addresses")
        connection = _PublicHTTPSConnection(parsed.hostname, addresses[0])
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            connection.request("GET", path, headers={"User-Agent": "Forma-OSS/1.0", "Accept": "image/png,image/jpeg,image/webp"})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ValueError("image redirect has no destination")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError("image download failed")
            content_type = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type not in IMAGE_CONTENT_TYPES:
                raise ValueError("unsupported image content type")
            length = response.getheader("Content-Length")
            if length is not None and not 0 < int(length) <= MAX_IMAGE_BYTES:
                raise ValueError("image exceeds size limit")
            content = response.read(MAX_IMAGE_BYTES + 1)
            if not content or len(content) > MAX_IMAGE_BYTES:
                raise ValueError("image is empty or exceeds size limit")
            return content, content_type
        finally:
            connection.close()
    raise ValueError("too many image redirects")


def materialize_image(image_data: str) -> tuple[str, str]:
    """Return validated inline image data for local or remote project storage."""
    if image_data.startswith("https://"):
        content, content_type = _download_image(image_data)
        return f"data:{content_type};base64,{base64.b64encode(content).decode('ascii')}", content_type
    header, encoded = image_data.split(",", 1)
    content_type = header.removeprefix("data:").removesuffix(";base64")
    if header != f"data:{content_type};base64" or content_type not in IMAGE_CONTENT_TYPES:
        raise ValueError("unsupported image response")
    if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise ValueError("image exceeds size limit")
    content = base64.b64decode(encoded, validate=True)
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise ValueError("image is empty or exceeds size limit")
    return image_data, content_type
