from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional


@dataclass
class Player:
    user_id: str
    nicknames: List[str]
    status: str = "unknown"
    ban_counts: int = 0
    ban_reasons: List[Dict[str, str]] = field(default_factory=list)
    connection_link: str = "N/A"
    associated_ips: Dict[str, List[str]] = field(default_factory=dict)
    associated_hwids: Dict[str, List[str]] = field(default_factory=dict)
    shared_hwid_nicknames: List[str] = field(default_factory=list)
    denied_logins: List[Dict[str, str]] = field(default_factory=list)
    hwid_erased: bool = False
    complaint_links: List[Dict[str, Any]] = field(default_factory=list)
    nicknames_sources: Dict[str, str] = field(default_factory=dict)
    raw_message: Optional[str] = None
    is_primary: bool = False
    _primary_nickname: Optional[str] = None

    @property
    def primary_nickname(self) -> str:
        if self._primary_nickname:
            return self._primary_nickname

        if not self.nicknames:
            return "Unknown"

        if self.is_primary:
            return self.nicknames[0]

        return self.nicknames[0]

    @primary_nickname.setter
    def primary_nickname(self, value: str) -> None:
        self._primary_nickname = value
