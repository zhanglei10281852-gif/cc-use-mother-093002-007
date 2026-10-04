"""申请与材料版本。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

ACTIVE = "ACTIVE"
MERGED_DUPLICATE = "MERGED_DUPLICATE"


@dataclass(frozen=True)
class MaterialVersion:
    """一份材料的特定版本；摘要用于防止事后替换。"""

    material_type: str
    version: int
    digest: str
    verified: bool = False

    def __post_init__(self) -> None:
        if not self.material_type or not self.digest:
            raise ValueError("材料类别与摘要不能为空")
        if self.version < 1:
            raise ValueError("材料版本号必须为正整数")


@dataclass(frozen=True)
class Application:
    """一份奖学金申请；身份信息只保存令牌，不保存明文证件号。"""

    application_id: str
    identity_token: str
    channel: str
    country: str
    program: str
    submitted_at: int
    materials: Tuple[MaterialVersion, ...]
    status: str = ACTIVE
    merged_into: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.application_id or not self.identity_token:
            raise ValueError("申请编号与身份令牌不能为空")
        if not self.channel or not self.country or not self.program:
            raise ValueError("报名渠道、国别与项目不能为空")
        object.__setattr__(self, "materials", tuple(self.materials))
        if self.status not in (ACTIVE, MERGED_DUPLICATE):
            raise ValueError("未知的申请状态")

    def verified_material_types(self) -> frozenset:
        return frozenset(m.material_type for m in self.materials if m.verified)


def material_to_dict(material: MaterialVersion) -> dict:
    return {
        "material_type": material.material_type,
        "version": material.version,
        "digest": material.digest,
        "verified": material.verified,
    }


def material_from_dict(data: Mapping) -> MaterialVersion:
    return MaterialVersion(
        material_type=data["material_type"],
        version=int(data["version"]),
        digest=data["digest"],
        verified=bool(data["verified"]),
    )


def application_to_dict(application: Application) -> dict:
    return {
        "application_id": application.application_id,
        "identity_token": application.identity_token,
        "channel": application.channel,
        "country": application.country,
        "program": application.program,
        "submitted_at": application.submitted_at,
        "materials": [material_to_dict(m) for m in application.materials],
        "status": application.status,
        "merged_into": application.merged_into,
    }


def application_from_dict(data: Mapping) -> Application:
    return Application(
        application_id=data["application_id"],
        identity_token=data["identity_token"],
        channel=data["channel"],
        country=data["country"],
        program=data["program"],
        submitted_at=int(data["submitted_at"]),
        materials=tuple(material_from_dict(item) for item in data["materials"]),
        status=data["status"],
        merged_into=data.get("merged_into"),
    )
