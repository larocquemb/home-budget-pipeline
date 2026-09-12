"""Normal (v1), reprocess (v2), and cache rebuild (v3) work contracts."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from uuid import UUID

MAX_ATTEMPTS = 3


class InvalidReceiptMessage(ValueError):
    """A message or its source cannot safely be processed."""


@dataclass(frozen=True)
class ReceiptMessage:
    source_sha256: str
    source_reference: str
    attempt: int = 1
    version: int = 1
    request_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version not in (1, 2, 3):
            raise InvalidReceiptMessage("unsupported receipt message version")
        if self.version == 1 and self.request_id is not None:
            raise InvalidReceiptMessage("normal receipt work cannot have a request_id")
        if self.version in (2, 3):
            try:
                if not isinstance(self.request_id, str) or str(UUID(self.request_id)) != self.request_id:
                    raise ValueError("noncanonical UUID")
            except ValueError as exc:
                raise InvalidReceiptMessage("reprocess request_id must be a canonical UUID") from exc
        if not isinstance(self.source_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", self.source_sha256):
            raise InvalidReceiptMessage("source_sha256 must be a lowercase SHA-256 digest")
        ref = self.source_reference
        if not isinstance(ref, str) or not ref or "\\" in ref or "\x00" in ref:
            raise InvalidReceiptMessage("source_reference must be a relative POSIX path")
        path = PurePosixPath(ref)
        if path.is_absolute() or any(part in ("", ".", "..") for part in ref.split("/")):
            raise InvalidReceiptMessage("source_reference must stay under the receipt root")
        if type(self.attempt) is not int or not 1 <= self.attempt <= MAX_ATTEMPTS:
            raise InvalidReceiptMessage("invalid attempt number")

    @property
    def message_id(self) -> str:
        if self.request_id:
            return f"{self.message_type}:{self.source_sha256}:{self.request_id}"
        return f"receipt.v1:{self.source_sha256}"

    @property
    def message_type(self) -> str:
        if self.version == 3:
            return "receipt.cache-rebuild.v3"
        return "receipt.reprocess.v2" if self.request_id else "receipt.process.v1"

    def to_bytes(self) -> bytes:
        payload = asdict(self)
        if self.version == 1:
            del payload["request_id"]
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_bytes(cls, body: bytes) -> ReceiptMessage:
        try:
            if len(body) > 8192:
                raise ValueError("message exceeds 8192 bytes")
            value = json.loads(body)
            fields = {"version", "source_sha256", "source_reference", "attempt"}
            if isinstance(value, dict) and value.get("version") in (2, 3):
                fields.add("request_id")
            if not isinstance(value, dict) or set(value) != fields:
                raise ValueError("unexpected message fields")
            return cls(**value)
        except (ValueError, TypeError, UnicodeError) as exc:
            raise InvalidReceiptMessage(str(exc)) from exc

    def resolve_source(self, root: Path) -> Path:
        root = root.resolve()
        path = (root / self.source_reference).resolve()
        if not path.is_relative_to(root):
            raise InvalidReceiptMessage("source_reference resolves outside the receipt root")
        return path
