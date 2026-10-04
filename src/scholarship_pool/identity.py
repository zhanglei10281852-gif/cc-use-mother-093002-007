"""申请人身份登记：以不可逆令牌代替明文证件号完成查重与合并。

设计要点：
- 令牌由登记处持有的密钥对规范化证件号做 HMAC 得出，无法反推明文；
- 事件日志、分配结果与对外报告中只出现令牌，不出现姓名与证件号；
- 规范申请人编号由令牌派生，重启后凭同一证件号仍得到同一身份，
  因此重复合并不依赖任何易失的内存状态。
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Dict, Optional


def _normalize_document(id_document: str) -> str:
    return "".join(id_document.split()).upper()


@dataclass(frozen=True)
class IdentityProfile:
    applicant_id: str
    token: str
    display_name: str


class IdentityRegistry:
    """登记身份并发放令牌；明文信息留在登记处内部，不参与分配与审计。"""

    def __init__(self, secret: bytes) -> None:
        if not secret:
            raise ValueError("身份登记密钥不能为空")
        self._secret = bytes(secret)
        self._profiles: Dict[str, IdentityProfile] = {}

    def token_for(self, id_document: str) -> str:
        if not id_document or not id_document.strip():
            raise ValueError("证件号不能为空")
        normalized = _normalize_document(id_document).encode("utf-8")
        return hmac.new(self._secret, normalized, hashlib.sha256).hexdigest()

    def register(self, *, id_document: str, display_name: str) -> IdentityProfile:
        """同一证件号无论经哪个渠道登记，都映射到同一身份。"""
        token = self.token_for(id_document)
        profile = self._profiles.get(token)
        if profile is None:
            profile = IdentityProfile(
                applicant_id=f"A-{token[:12]}",
                token=token,
                display_name=display_name,
            )
            self._profiles[token] = profile
        return profile

    def display_name_for(self, token: str) -> Optional[str]:
        profile = self._profiles.get(token)
        return None if profile is None else profile.display_name
