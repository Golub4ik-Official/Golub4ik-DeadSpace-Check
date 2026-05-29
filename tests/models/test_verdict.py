from models.verdict import VerdictCategory, ConfidenceLevel, Verdict


class TestVerdictCategory:
    def test_values(self):
        assert VerdictCategory.BANNED.value == "BANNED"
        assert VerdictCategory.CLEAN.value == "CLEAN"
        assert VerdictCategory.SUSPICIOUS.value == "SUSPICIOUS"
        assert VerdictCategory.POTENTIAL_BYPASS.value == "POTENTIAL BYPASS"
        assert VerdictCategory.UNKNOWN.value == "UNKNOWN"

    def test_members(self):
        assert len(VerdictCategory) == 5


class TestConfidenceLevel:
    def test_values(self):
        assert "100%" in ConfidenceLevel.HWID_MATCH.value
        assert "80-90%" in ConfidenceLevel.IP_VERY_CLOSE_TIME.value
        assert "No Match" in ConfidenceLevel.NO_MATCH.value

    def test_members(self):
        assert len(ConfidenceLevel) == 7


class TestVerdict:
    def test_minimal(self):
        v = Verdict(category=VerdictCategory.CLEAN)
        assert v.category == VerdictCategory.CLEAN
        assert v.confidence is None
        assert v.reason is None
        assert v.hwid_erased is False

    def test_full(self):
        v = Verdict(
            category=VerdictCategory.POTENTIAL_BYPASS,
            confidence=ConfidenceLevel.HWID_MATCH,
            reason="Same HWID",
            hwid_erased=True,
        )
        assert v.category == VerdictCategory.POTENTIAL_BYPASS
        assert v.confidence == ConfidenceLevel.HWID_MATCH
        assert v.reason == "Same HWID"
        assert v.hwid_erased is True

    def test_str_basic(self):
        v = Verdict(category=VerdictCategory.CLEAN)
        assert str(v) == "CLEAN"

    def test_str_with_confidence(self):
        v = Verdict(category=VerdictCategory.BANNED, confidence=ConfidenceLevel.HWID_MATCH)
        assert "BANNED" in str(v)
        assert "100%" in str(v)

    def test_str_with_reason(self):
        v = Verdict(category=VerdictCategory.SUSPICIOUS, reason="IP match")
        assert "SUSPICIOUS" in str(v)
        assert "IP match" in str(v)

    def test_str_with_hwid_erased(self):
        v = Verdict(category=VerdictCategory.UNKNOWN, hwid_erased=True)
        assert str(v) == "UNKNOWN / HWID Erased"

    def test_str_full(self):
        v = Verdict(
            category=VerdictCategory.POTENTIAL_BYPASS,
            confidence=ConfidenceLevel.IP_CLOSE_TIME,
            reason="IP + close time",
            hwid_erased=True,
        )
        s = str(v)
        assert "POTENTIAL BYPASS" in s
        assert "60-70%" in s
        assert "IP + close time" in s
        assert "HWID Erased" in s
