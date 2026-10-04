"""追加式事件日志：审计重放的唯一事实来源。

日志按行写入 JSON，序号严格连续；加载时校验连续性，
任何中间缺行都会立即暴露。日志中只出现身份令牌，不出现明文个人信息。
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Mapping, Optional

RULESET_COMMITTED = "RULESET_COMMITTED"
ROUND_OPENED = "ROUND_OPENED"
APPLICATION_SUBMITTED = "APPLICATION_SUBMITTED"
APPLICATION_MERGED = "APPLICATION_MERGED"
CONFLICT_DECLARED = "CONFLICT_DECLARED"
SCORE_SUBMITTED = "SCORE_SUBMITTED"
ALLOCATION_COMPUTED = "ALLOCATION_COMPUTED"
ADMISSION_CONFIRMED = "ADMISSION_CONFIRMED"
ADMISSION_WITHDRAWN = "ADMISSION_WITHDRAWN"
ADMISSION_REVOKED = "ADMISSION_REVOKED"
CANDIDATE_PROMOTED = "CANDIDATE_PROMOTED"
APPEAL_FILED = "APPEAL_FILED"
APPEAL_RESOLVED = "APPEAL_RESOLVED"
SLOT_FROZEN = "SLOT_FROZEN"
SLOT_UNFROZEN = "SLOT_UNFROZEN"
DECISION_REVERSED = "DECISION_REVERSED"
CANDIDATE_REINSTATED = "CANDIDATE_REINSTATED"


@dataclass(frozen=True)
class Event:
    seq: int
    kind: str
    round_id: Optional[str]
    payload: Mapping

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "round_id": self.round_id,
            "payload": self.payload,
        }

    @staticmethod
    def from_dict(data: Mapping) -> "Event":
        return Event(
            seq=int(data["seq"]),
            kind=data["kind"],
            round_id=data.get("round_id"),
            payload=data["payload"],
        )


class EventLog:
    """线程安全的追加式日志；重启后从磁盘恢复。"""

    def __init__(self, path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._events: List[Event] = []
        if self._path.exists():
            with open(self._path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        self._events.append(Event.from_dict(json.loads(line)))
            for index, event in enumerate(self._events, 1):
                if event.seq != index:
                    raise ValueError("事件日志序号不连续，可能被篡改")

    def append(self, kind: str, round_id: Optional[str], payload: Mapping) -> Event:
        with self._lock:
            event = Event(len(self._events) + 1, kind, round_id, payload)
            line = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._events.append(event)
            return event

    def __iter__(self) -> Iterator[Event]:
        return iter(tuple(self._events))

    def __len__(self) -> int:
        return len(self._events)

    def for_round(self, round_id: str) -> List[Event]:
        return [event for event in self._events if event.round_id == round_id]
