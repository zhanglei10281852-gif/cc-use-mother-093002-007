"""申请档案与材料版本。

每份申请属于一个身份、一个国别、一个项目，并携带不可变的材料版本：
材料每次更新都产生新版本号与内容哈希，分配轮次固定引用某一版本，
避免“评审期间偷偷换材料”。资格状态可被授予或撤销，撤销必须留因。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import canonical_json, sha256_hex
from .errors import NotFoundError, RuleRevisionError, ValidationError

ELIGIBLE = "eligible"
WITHDRAWN = "withdrawn"
DISQUALIFIED = "disqualified"
SUPERSEDED = "superseded"


@dataclass(frozen=True)
class MaterialVersion:
    version: int
    content_hash: str
    fields: tuple[tuple[str, str], ...]


@dataclass
class Application:
    application_id: str
    person_id: str
    country: str
    program_id: str
    channel: str
    material_versions: list[MaterialVersion] = field(default_factory=list)
    status: str = ELIGIBLE
    status_reason: str = ""

    @property
    def current_material(self) -> MaterialVersion:
        return self.material_versions[-1]


class ApplicationRegistry:
    def __init__(self) -> None:
        self._apps: dict[str, Application] = {}

    def submit(
        self,
        application_id: str,
        person_id: str,
        country: str,
        program_id: str,
        channel: str,
        material_fields: dict[str, str],
    ) -> MaterialVersion:
        if application_id in self._apps:
            raise ValidationError(f"申请 {application_id} 已存在")
        app = Application(
            application_id=application_id,
            person_id=person_id,
            country=country,
            program_id=program_id,
            channel=channel,
        )
        self._apps[application_id] = app
        return self._append_material(app, material_fields)

    def _append_material(
        self, app: Application, material_fields: dict[str, str]
    ) -> MaterialVersion:
        missing = [k for k, v in material_fields.items() if v in (None, "")]
        if missing:
            raise ValidationError(f"材料字段缺失：{sorted(missing)}")
        ordered = tuple(sorted(material_fields.items()))
        content_hash = sha256_hex(canonical_json(list(ordered)))
        if app.material_versions and app.material_versions[-1].content_hash == content_hash:
            raise ValidationError("新材料与当前版本内容相同，无需产生新版本")
        version = MaterialVersion(
            version=len(app.material_versions) + 1,
            content_hash=content_hash,
            fields=ordered,
        )
        app.material_versions.append(version)
        return version

    def update_material(
        self, application_id: str, material_fields: dict[str, str]
    ) -> MaterialVersion:
        app = self.get(application_id)
        return self._append_material(app, material_fields)

    def get(self, application_id: str) -> Application:
        try:
            return self._apps[application_id]
        except KeyError:
            raise NotFoundError(f"申请 {application_id} 不存在") from None

    def material_at(self, application_id: str, version: int) -> MaterialVersion:
        app = self.get(application_id)
        if version < 1 or version > len(app.material_versions):
            raise RuleRevisionError(f"申请 {application_id} 不存在材料版本 {version}")
        return app.material_versions[version - 1]

    def all(self) -> list[Application]:
        return [self._apps[k] for k in sorted(self._apps)]

    def withdraw(self, application_id: str, reason: str = "") -> None:
        app = self.get(application_id)
        app.status = WITHDRAWN
        app.status_reason = reason or "申请人放弃"

    def disqualify(self, application_id: str, reason: str) -> None:
        if not reason:
            raise ValidationError("资格撤销必须记录原因")
        app = self.get(application_id)
        app.status = DISQUALIFIED
        app.status_reason = reason

    def supersede(self, application_id: str, merged_into: str) -> None:
        app = self.get(application_id)
        app.status = SUPERSEDED
        app.status_reason = f"重复身份合并，申请并入 {merged_into}"

    def is_live(self, application_id: str) -> bool:
        return self.get(application_id).status == ELIGIBLE
