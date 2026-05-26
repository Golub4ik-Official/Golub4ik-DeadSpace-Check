from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional


@dataclass
class ComplaintMessage:
    id: str
    content: str
    embeds: List[Dict[str, Any]]
    channel_id: str
    guild_id: str
    mentioned_nicknames: List[str] = field(default_factory=list)

    @property
    def link(self) -> str:
        return f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{self.id}"


@dataclass
class ComplaintChannel:
    id: str
    name: str
    guild_id: str
    messages: List[ComplaintMessage] = field(default_factory=list)
    last_cached_id: Optional[str] = None
