from dataclasses import dataclass, field
from datetime import datetime
from typing import List
from .player import Player


@dataclass
class DiscordMessage:
    id: str
    author_name: str
    author_id: str
    content: str
    channel_id: str
    guild_id: str
    created_at: datetime
    embed_titles: List[str] = field(default_factory=list)
    embed_links: List[str] = field(default_factory=list)

    @property
    def link(self) -> str:
        return f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{self.id}"


@dataclass
class ScanResult:
    message: DiscordMessage
    players: List[Player]
    scan_time: datetime = field(default_factory=datetime.now)
