from unittest.mock import patch, MagicMock

import pytest

from core.analyzer import PlayerAnalyzer
from models.player import Player


@pytest.fixture
def analyzer():
    mock_cfg = MagicMock()
    mock_cfg.confidence_levels.hwid_match = "HWID_MATCH"
    mock_cfg.confidence_levels.ip_very_close_time = "IP_VERY_CLOSE_TIME"
    mock_cfg.confidence_levels.ip_close_time = "IP_CLOSE_TIME"
    mock_cfg.confidence_levels.ip_moderate_time = "IP_MODERATE_TIME"
    mock_cfg.confidence_levels.ip_distant_time = "IP_DISTANT_TIME"
    mock_cfg.confidence_levels.ip_match = "IP_MATCH"
    mock_cfg.confidence_levels.no_match = "NO_MATCH"

    with patch("core.analyzer.get_config", return_value=mock_cfg):
        yield PlayerAnalyzer()


class TestGroupPlayersByNicknames:
    def test_empty_list(self, analyzer):
        assert analyzer.group_players_by_nicknames([]) == []

    def test_single_player(self, analyzer):
        p = Player(user_id="1", nicknames=["PlayerOne"])
        result = analyzer.group_players_by_nicknames([p])
        assert len(result) == 1
        assert result[0].user_id == "1"
        assert result[0].nicknames == ["PlayerOne"]

    def test_no_shared_nicknames(self, analyzer):
        p1 = Player(user_id="1", nicknames=["Alpha"])
        p2 = Player(user_id="2", nicknames=["Beta"])
        result = analyzer.group_players_by_nicknames([p1, p2])
        assert len(result) == 2

    def test_shared_nicknames_merged(self, analyzer):
        p1 = Player(user_id="1", nicknames=["SharedName", "Alpha"])
        p2 = Player(user_id="2", nicknames=["SharedName", "Beta"])
        result = analyzer.group_players_by_nicknames([p1, p2])
        assert len(result) == 1
        merged = result[0]
        assert set(merged.nicknames) == {"SharedName", "Alpha", "Beta"}

    def test_transitive_merge(self, analyzer):
        p1 = Player(user_id="1", nicknames=["A", "B"])
        p2 = Player(user_id="2", nicknames=["B", "C"])
        p3 = Player(user_id="3", nicknames=["C", "D"])
        result = analyzer.group_players_by_nicknames([p1, p2, p3])
        assert len(result) == 1
        assert set(result[0].nicknames) == {"A", "B", "C", "D"}

    def test_two_separate_groups(self, analyzer):
        p1 = Player(user_id="1", nicknames=["A", "B"])
        p2 = Player(user_id="2", nicknames=["B"])
        p3 = Player(user_id="3", nicknames=["C"])
        p4 = Player(user_id="4", nicknames=["C", "D"])
        result = analyzer.group_players_by_nicknames([p1, p2, p3, p4])
        assert len(result) == 2


class TestMergePlayerGroup:
    def test_single_player_returns_base(self, analyzer):
        p = Player(user_id="1", nicknames=["Test"])
        result = analyzer._merge_player_group([p])
        assert result is p

    def test_merges_nicknames(self, analyzer):
        p1 = Player(user_id="1", nicknames=["A"])
        p2 = Player(user_id="2", nicknames=["B"])
        result = analyzer._merge_player_group([p1, p2])
        assert set(result.nicknames) == {"A", "B"}

    def test_merges_ips(self, analyzer):
        p1 = Player(user_id="1", nicknames=["A"], associated_ips={"1.1.1.1": ["A"]})
        p2 = Player(user_id="2", nicknames=["B"], associated_ips={"2.2.2.2": ["B"]})
        result = analyzer._merge_player_group([p1, p2])
        assert "1.1.1.1" in result.associated_ips
        assert "2.2.2.2" in result.associated_ips

    def test_merges_ips_with_same_ip(self, analyzer):
        p1 = Player(user_id="1", nicknames=["A"], associated_ips={"1.1.1.1": ["A"]})
        p2 = Player(user_id="2", nicknames=["B"], associated_ips={"1.1.1.1": ["B"]})
        result = analyzer._merge_player_group([p1, p2])
        assert set(result.associated_ips["1.1.1.1"]) == {"A", "B"}

    def test_merges_hwids(self, analyzer):
        p1 = Player(user_id="1", nicknames=["A"], associated_hwids={"hwid1": ["A"]})
        p2 = Player(user_id="2", nicknames=["B"], associated_hwids={"hwid2": ["B"]})
        result = analyzer._merge_player_group([p1, p2])
        assert "hwid1" in result.associated_hwids
        assert "hwid2" in result.associated_hwids

    def test_highest_status_wins(self, analyzer):
        p1 = Player(user_id="1", nicknames=["A"], status="clean")
        p2 = Player(user_id="2", nicknames=["B"], status="banned")
        result = analyzer._merge_player_group([p1, p2])
        assert result.status == "banned"

    def test_status_priority(self, analyzer):
        p1 = Player(user_id="1", nicknames=["A"], status="unknown")
        p2 = Player(user_id="2", nicknames=["B"], status="suspicious")
        result = analyzer._merge_player_group([p1, p2])
        assert result.status == "suspicious"

    def test_max_ban_count(self, analyzer):
        p1 = Player(user_id="1", nicknames=["A"], ban_counts=2)
        p2 = Player(user_id="2", nicknames=["B"], ban_counts=5)
        result = analyzer._merge_player_group([p1, p2])
        assert result.ban_counts == 5

    def test_merges_ban_reasons(self, analyzer):
        p1 = Player(user_id="1", nicknames=["A"],
                     ban_reasons=[{"reason": "Grief", "username": "A"}])
        p2 = Player(user_id="2", nicknames=["B"],
                     ban_reasons=[{"reason": "Exploit", "username": "B"}])
        result = analyzer._merge_player_group([p1, p2])
        assert len(result.ban_reasons) == 2

    def test_deduplicates_ban_reasons(self, analyzer):
        p1 = Player(user_id="1", nicknames=["A"],
                     ban_reasons=[{"reason": "Grief", "username": "A"}])
        p2 = Player(user_id="2", nicknames=["B"],
                     ban_reasons=[{"reason": "Grief", "username": "A"}])
        result = analyzer._merge_player_group([p1, p2])
        assert len(result.ban_reasons) == 1

    def test_hwid_erased_propagates(self, analyzer):
        p1 = Player(user_id="1", nicknames=["A"], hwid_erased=False)
        p2 = Player(user_id="2", nicknames=["B"], hwid_erased=True)
        result = analyzer._merge_player_group([p1, p2])
        assert result.hwid_erased is True

    def test_merges_shared_hwid_nicknames(self, analyzer):
        p1 = Player(user_id="1", nicknames=["A"], shared_hwid_nicknames=["X"])
        p2 = Player(user_id="2", nicknames=["B"], shared_hwid_nicknames=["Y"])
        result = analyzer._merge_player_group([p1, p2])
        assert set(result.shared_hwid_nicknames) == {"X", "Y"}

    def test_empty_group_returns_none(self, analyzer):
        assert analyzer._merge_player_group([]) is None
