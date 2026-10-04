"""仅追加（append-only）的事件日志。

所有治理命令都序列化为带全局序号的事件，事件之间以 SHA-256 哈希链相连：
后一事件的哈希包含前一事件哈希，离线重放时逐链校验，任何删改都会暴露。
重启后按序号重放即可还原全部状态；时间戳不参与任何排序或哈希输入，
因此重放结果与首次执行完全一致。
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import canonical_json, sha256_hex

GENESIS = "0" * 64


@dataclass(frozen=True)
class Event:
    seq: int
    ts: str
    type: str
    payload: dict[str, Any]
    round_id: str | None = None
    prev_hash: str = GENESIS
    event_hash: str = ""

    def to_line(self) -> str:
        return json.dumps(
            {
                "seq": self.seq,
                "ts": self.ts,
                "type": self.type,
                "round_id": self.round_id,
                "prev_hash": self.prev_hash,
                "event_hash": self.event_hash,
                "payload": self.payload,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_line(cls, line: str) -> "Event":
        data = json.loads(line)
        return cls(
            seq=data["seq"],
            ts=data["ts"],
            type=data["type"],
            payload=data["payload"],
            round_id=data.get("round_id"),
            prev_hash=data.get("prev_hash", GENESIS),
            event_hash=data.get("event_hash", ""),
        )

    def digest_input(self) -> bytes:
        body = [self.seq, self.ts, self.type, self.round_id, self.payload]
        return canonical_json([self.prev_hash, body])


class EventStore:
    """文件型或内存型事件日志；追加与读取均在锁内完成。"""

    def __init__(self, path: str | Path | None = None, verify: bool = True) -> None:
        self._lock = threading.RLock()
        self._path = Path(path) if path is not None else None
        self._events: list[Event] = []
        if self._path is not None and self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._events.append(Event.from_line(line))
            seqs = [e.seq for e in self._events]
            if seqs != list(range(1, len(seqs) + 1)):
                raise ValueError("事件日志序号不连续，无法安全重放")
            if verify:
                self.verify_chain()

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def head_hash(self) -> str:
        return self._events[-1].event_hash if self._events else GENESIS

    def verify_chain(self) -> None:
        prev = GENESIS
        for event in self._events:
            if event.prev_hash != prev:
                raise ValueError(f"事件 {event.seq} 哈希链断裂")
            expected = sha256_hex(event.digest_input())
            if event.event_hash != expected:
                raise ValueError(f"事件 {event.seq} 内容哈希不匹配，日志可能被篡改")
            prev = event.event_hash

    def append(
        self, event_type: str, payload: dict[str, Any], round_id: str | None = None
    ) -> Event:
        with self._lock:
            draft = Event(
                seq=len(self._events) + 1,
                ts=datetime.now(timezone.utc).isoformat(),
                type=event_type,
                payload=payload,
                round_id=round_id,
                prev_hash=self.head_hash,
            )
            event = replace(draft, event_hash=sha256_hex(draft.digest_input()))
            if self._path is not None:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(event.to_line() + "\n")
                    fh.flush()
            self._events.append(event)
            return event

    def read(self) -> list[Event]:
        with self._lock:
            return list(self._events)
