"""申请人身份登记与重复身份合并。

同一自然人可能通过不同渠道（合作院校、线上平台等）重复报名。
合并只在秘书处确认下进行：
- 每个申请人生成随机假名 ``A-xxxx``，对外事件只暴露假名；
- 渠道原始账号、证件等身份标识仅保存在身份登记册内部；
- 合并为单向操作，被合并的申请编号归入同一身份且不可拆分；
- 冲突的材料字段必须显式取舍，不能静默选择。
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from .errors import NotFoundError, ValidationError


@dataclass
class Identity:
    person_id: str
    display_pseudonym: str
    application_ids: set[str] = field(default_factory=set)
    # 合并到的目标身份；非 None 时本身份已失效。
    merged_into: str | None = None


class IdentityRegistry:
    def __init__(self) -> None:
        # application_id -> 主身份 id
        self._app_owner: dict[str, str] = {}
        self._identities: dict[str, Identity] = {}
        # 仅内部可见的身份属性，绝不进入对外审计视图
        self._sensitive: dict[str, dict[str, str]] = {}

    # ---- 登记 ----
    def register(self, application_id: str, sensitive: dict[str, str]) -> str:
        if not application_id:
            raise ValidationError("申请编号为空")
        if application_id in self._app_owner:
            raise ValidationError(f"申请 {application_id} 已登记身份")
        person_id = "P-" + secrets.token_hex(6)
        pseudonym = "A-" + secrets.token_hex(4)
        self._identities[person_id] = Identity(
            person_id=person_id,
            display_pseudonym=pseudonym,
            application_ids={application_id},
        )
        self._app_owner[application_id] = person_id
        self._sensitive[person_id] = dict(sensitive)
        return person_id

    def owner_of(self, application_id: str) -> str:
        try:
            return self._resolve(self._app_owner[application_id])
        except KeyError:
            raise NotFoundError(f"申请 {application_id} 未登记") from None

    def _resolve(self, person_id: str) -> str:
        seen: set[str] = set()
        while True:
            if person_id in seen:
                raise ValidationError("身份合并链存在环路")
            seen.add(person_id)
            target = self._identities[person_id].merged_into
            if target is None:
                return person_id
            person_id = target

    def pseudonym(self, person_id: str) -> str:
        return self._identities[self._resolve(person_id)].display_pseudonym

    def pseudonym_for_application(self, application_id: str) -> str:
        return self.pseudonym(self.owner_of(application_id))

    def application_pseudonym_map(self) -> dict[str, str]:
        """供评分/分配视图使用：申请编号 → 假名，不含任何证件信息。"""
        return {
            app_id: self.pseudonym(pid) for app_id, pid in self._app_owner.items()
        }

    def duplicates_of(self, person_id: str) -> set[str]:
        return set(self._identities[self._resolve(person_id)].application_ids)

    # ---- 合并 ----
    def merge(self, source_application_id: str, target_application_id: str) -> str:
        """把两个申请背后的身份合并为一个，返回合并后主身份。"""
        source = self.owner_of(source_application_id)
        target = self.owner_of(target_application_id)
        if source == target:
            raise ValidationError("两个申请已属于同一身份，无需合并")
        source_identity = self._identities[source]
        target_identity = self._identities[target]
        target_identity.application_ids.update(source_identity.application_ids)
        source_identity.merged_into = target
        source_identity.application_ids.clear()
        return target

    def public_view(self) -> dict[str, dict]:
        """审计/对外视图：只含假名与其名下申请编号。"""
        view: dict[str, dict] = {}
        for pid, identity in self._identities.items():
            if identity.merged_into is not None:
                continue
            view[identity.display_pseudonym] = {
                "applications": sorted(identity.application_ids),
            }
        return view

    def to_snapshot(self) -> dict:
        return {
            pid: {
                "pseudonym": identity.display_pseudonym,
                "applications": sorted(identity.application_ids),
                "merged_into": identity.merged_into,
            }
            for pid, identity in self._identities.items()
        }

    def load_snapshot(self, data: dict, app_owner: dict[str, str]) -> None:
        self._identities.clear()
        self._sensitive.clear()
        for pid, row in data.items():
            self._identities[pid] = Identity(
                person_id=pid,
                display_pseudonym=row["pseudonym"],
                application_ids=set(row["applications"]),
                merged_into=row.get("merged_into"),
            )
        self._app_owner = dict(app_owner)
