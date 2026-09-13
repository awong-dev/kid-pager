
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PublishedMessage:
    topic: str
    payload: bytes
    qos: int
    retain: bool










