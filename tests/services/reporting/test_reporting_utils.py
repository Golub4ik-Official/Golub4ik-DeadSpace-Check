from models.player import Player

from services.reporting.utils import (
    determine_owner,
    analyze_hwids,
    analyze_ips,
    find_connection_paths,
)


class TestDetermineOwner:
    def test_primary_in_shared_with(self):
        result = determine_owner("Main", ["Main", "Alt"], ["Main", "Other"])
        assert result == "Main"

    def test_nick_in_shared_with(self):
        result = determine_owner("Main", ["Alt1", "Alt2"], ["Alt1"])
        assert result == "Alt1"

    def test_first_shared_when_no_match(self):
        result = determine_owner("Main", ["Alt1"], ["Shared1", "Shared2"])
        assert result == "Shared1"

    def test_unknown_when_no_shared(self):
        result = determine_owner("Main", ["Alt1"], [])
        assert result == "Unknown"

    def test_uses_cache(self):
        cache = {}
        result1 = determine_owner("Main", ["A", "B"], ["A", "C"], cache)
        result2 = determine_owner("Main", ["A", "B"], ["A", "C"], cache)
        assert result1 == result2


class TestAnalyzeHwids:
    def test_owned_hwid(self):
        player = Player(
            user_id="1", nicknames=["Main", "Alt"],
            associated_hwids={"hwid1": ["Main", "Other"]},
        )
        owned, alt, other = analyze_hwids(player, "Main")
        assert len(owned) == 1
        assert owned[0][0] == "hwid1"

    def test_alt_hwid(self):
        player = Player(
            user_id="1", nicknames=["Main", "Alt"],
            associated_hwids={"hwid2": ["Alt", "Stranger"]},
        )
        owned, alt, other = analyze_hwids(player, "Main")
        assert len(alt) == 1
        assert alt[0][0] == "hwid2"

    def test_other_hwid(self):
        player = Player(
            user_id="1", nicknames=["Main"],
            associated_hwids={"hwid3": ["Stranger1", "Stranger2"]},
        )
        owned, alt, other = analyze_hwids(player, "Main")
        assert len(other) == 1
        assert other[0][0] == "hwid3"

    def test_empty_hwids(self):
        player = Player(user_id="1", nicknames=["Main"])
        owned, alt, other = analyze_hwids(player, "Main")
        assert owned == []
        assert alt == []
        assert other == []

    def test_sort_by_shared_count(self):
        player = Player(
            user_id="1", nicknames=["Main"],
            associated_hwids={
                "hwid_a": ["Main", "A", "B"],
                "hwid_b": ["Main", "C"],
            },
        )
        owned, alt, other = analyze_hwids(player, "Main")
        assert owned[0][0] == "hwid_a"
        assert owned[1][0] == "hwid_b"


class TestAnalyzeIps:
    def test_original_ip(self):
        player = Player(
            user_id="1", nicknames=["Main"],
            associated_ips={"1.1.1.1": ["Main"]},
        )
        orig, shared, alt, multi = analyze_ips(player, "Main")
        assert orig == ["1.1.1.1"]
        assert shared == []
        assert alt == []
        assert multi == []

    def test_shared_ip(self):
        player = Player(
            user_id="1", nicknames=["Main"],
            associated_ips={"1.1.1.1": ["Main", "Other"]},
        )
        orig, shared, alt, multi = analyze_ips(player, "Main")
        assert orig == []
        assert len(shared) == 1
        assert shared[0][0] == "1.1.1.1"

    def test_alt_shared_ip(self):
        player = Player(
            user_id="1", nicknames=["Main", "Alt"],
            associated_ips={"1.1.1.1": ["Alt", "Stranger"]},
        )
        orig, shared, alt, multi = analyze_ips(player, "Main")
        assert len(alt) == 1
        assert alt[0][0] == "1.1.1.1"

    def test_multi_user_ip(self):
        player = Player(
            user_id="1", nicknames=["Main"],
            associated_ips={"1.1.1.1": ["A", "B"]},
        )
        orig, shared, alt, multi = analyze_ips(player, "Main")
        assert len(multi) == 1
        assert multi[0][0] == "1.1.1.1"

    def test_empty_ips(self):
        player = Player(user_id="1", nicknames=["Main"])
        orig, shared, alt, multi = analyze_ips(player, "Main")
        assert orig == []
        assert shared == []
        assert alt == []
        assert multi == []


class TestFindConnectionPaths:
    def test_no_nicknames_returns_none(self):
        player = Player(user_id="1", nicknames=[])
        assert find_connection_paths(player, "Main") is None

    def test_single_nickname_returns_none(self):
        player = Player(user_id="1", nicknames=["Main"])
        assert find_connection_paths(player, "Main") is None

    def test_no_hwids_or_ips_returns_none(self):
        player = Player(user_id="1", nicknames=["Main", "Alt"])
        assert find_connection_paths(player, "Main") is None

    def test_direct_hwid_connection(self):
        player = Player(
            user_id="1", nicknames=["Main", "Alt"],
            associated_hwids={"hwid1": ["Main", "Alt"]},
        )
        result = find_connection_paths(player, "Main")
        assert result is not None
        assert "Alt" in result["direct_connections"]
        assert result["direct_connections"]["Alt"]["type"] == "HWID"

    def test_direct_ip_connection(self):
        player = Player(
            user_id="1", nicknames=["Main", "Alt"],
            associated_ips={"1.1.1.1": ["Main", "Alt"]},
        )
        result = find_connection_paths(player, "Main")
        assert result is not None
        assert "Alt" in result["direct_connections"]
        assert result["direct_connections"]["Alt"]["type"] == "IP"

    def test_indirect_connection(self):
        player = Player(
            user_id="1", nicknames=["Main", "Bridge", "Target"],
            associated_hwids={
                "hwid1": ["Main", "Bridge"],
                "hwid2": ["Bridge", "Target"],
            },
        )
        result = find_connection_paths(player, "Main")
        assert result is not None
        assert "Bridge" in result["direct_connections"]
        assert "Target" in result["indirect_connections"]

    def test_output_structure(self, sample_player):
        result = find_connection_paths(sample_player, "TestPlayer")
        assert result is not None
        assert "direct_connections" in result
        assert "indirect_connections" in result
        assert "indirect_by_via" in result
