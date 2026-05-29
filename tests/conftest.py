from datetime import datetime
from typing import List, Dict, Any
import pytest

from models.player import Player
from models.message import DiscordMessage, ScanResult
from models.complaint import ComplaintMessage, ComplaintChannel
from models.verdict import VerdictCategory, ConfidenceLevel, Verdict


@pytest.fixture
def sample_player() -> Player:
    return Player(
        user_id="12345",
        nicknames=["TestPlayer", "AltNick"],
        status="clean",
        ban_counts=0,
        ban_reasons=[],
        associated_ips={
            "192.168.1.1": ["TestPlayer"],
            "10.0.0.1": ["TestPlayer", "OtherPlayer"],
        },
        associated_hwids={
            "hwid_abc123": ["TestPlayer", "AltNick"],
            "hwid_def456": ["AltNick", "AnotherAlt"],
        },
        shared_hwid_nicknames=["AltNick", "AnotherAlt"],
        denied_logins=[
            {"user_name": "Intruder", "time": "2024-01-15 10:30:00"}
        ],
        complaint_links=[
            {"content": "Complaint about TestPlayer", "link": "http://example.com/1",
             "mentioned_nicknames": ["TestPlayer"]},
            {"content": "Another complaint", "link": "http://example.com/2",
             "mentioned_nicknames": ["AltNick"]},
        ],
        nicknames_sources={"TestPlayer": {"type": "primary"}, "AltNick": {"type": "nickname"}},
        is_primary=True,
    )


@pytest.fixture
def sample_discord_message() -> DiscordMessage:
    return DiscordMessage(
        id="987654321",
        author_name="AuthorUser",
        author_id="11111",
        content="Check this player: TestPlayer",
        channel_id="22222",
        guild_id="33333",
        created_at=datetime(2024, 1, 1, 12, 0, 0),
        embed_titles=["Player Info"],
        embed_links=["https://admin.deadspace14.net/Connections?search=TestPlayer"],
    )


@pytest.fixture
def sample_complaint_message() -> ComplaintMessage:
    return ComplaintMessage(
        id="555555",
        content="Player TestPlayer is breaking rules",
        embeds=[{"title": "Evidence", "url": "http://example.com"}],
        channel_id="44444",
        guild_id="33333",
        mentioned_nicknames=["TestPlayer"],
    )


@pytest.fixture
def sample_complaint_channel(sample_complaint_message) -> ComplaintChannel:
    return ComplaintChannel(
        id="44444",
        name="complaints",
        guild_id="33333",
        messages=[sample_complaint_message],
        last_cached_id="555555",
    )


@pytest.fixture
def sample_scan_result(sample_discord_message) -> ScanResult:
    players = [
        Player(user_id="12345", nicknames=["TestPlayer"], status="clean"),
        Player(user_id="67890", nicknames=["Suspect"], status="banned", ban_counts=3),
    ]
    return ScanResult(
        message=sample_discord_message,
        players=players,
        scan_time=datetime(2024, 1, 1, 12, 0, 0),
    )
