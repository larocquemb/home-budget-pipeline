"""Version 1 receipt work contract; paths are relative to the shared receipt root."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

MAX_ATTEMPTS = 3


class InvalidReceiptMessage(ValueError):
    """A message or its source cannot safely be processed."""


@dataclass(frozen=True)
class ReceiptMessage:
    source_sha256: str
    source_reference: str
    attempt: int = 1
    version: int = 1

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise InvalidReceiptMessage("unsupported receipt message version")
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
        return f"receipt.v1:{self.source_sha256}"

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_bytes(cls, body: bytes) -> ReceiptMessage:
        try:
            if len(body) > 8192:
                raise ValueError("message exceeds 8192 bytes")
            value = json.loads(body)
            if not isinstance(value, dict) or set(value) != {"version", "source_sha256", "source_reference", "attempt"}:
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
