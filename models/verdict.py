from dataclasses import dataclass
from enum import Enum
from typing import Optional


class VerdictCategory(Enum):
    BANNED = "BANNED"
    CLEAN = "CLEAN"
    SUSPICIOUS = "SUSPICIOUS"
    POTENTIAL_BYPASS = "POTENTIAL BYPASS"
    UNKNOWN = "UNKNOWN"


class ConfidenceLevel(Enum):
    HWID_MATCH = "100% (HWID Match)"
    IP_VERY_CLOSE_TIME = "80-90% (IP + Very Close Time, <5min)"
    IP_CLOSE_TIME = "60-70% (IP + Close Time, 5-10min)"
    IP_MODERATE_TIME = "40-50% (IP + Moderate Time, 10-30min)"
    IP_DISTANT_TIME = "20-30% (IP + Distant Time, 30-60min)"
    IP_MATCH = "10-20% (IP Match)"
    NO_MATCH = "No Match Found"


@dataclass
class Verdict:
    category: VerdictCategory
    confidence: Optional[ConfidenceLevel] = None
    reason: Optional[str] = None
    hwid_erased: bool = False

    def __str__(self) -> str:
        base = f"{self.category.value}"
        if self.confidence:
            base += f" - {self.confidence.value}"
        if self.reason:
            base += f" - {self.reason}"
        if self.hwid_erased:
            base += " / HWID Erased"
        return base