from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from constants import (
    VERY_LOW, LOW, HIGH, VERY_HIGH,
    COLOR_VERY_LOW, COLOR_LOW, COLOR_NORMAL, COLOR_HIGH, COLOR_VERY_HIGH,
)


@dataclass
class GlucoseReading:
    value: int
    trend_arrow: str
    trend_description: str
    timestamp: datetime

    @property
    def status(self) -> str:
        if self.value < VERY_LOW:
            return "very_low"
        elif self.value < LOW:
            return "low"
        elif self.value <= HIGH:
            return "normal"
        elif self.value <= VERY_HIGH:
            return "high"
        else:
            return "very_high"

    @property
    def color_hex(self) -> str:
        return {
            "very_low": COLOR_VERY_LOW,
            "low": COLOR_LOW,
            "normal": COLOR_NORMAL,
            "high": COLOR_HIGH,
            "very_high": COLOR_VERY_HIGH,
        }[self.status]

    @property
    def status_text(self) -> str:
        return {
            "very_low": "Very Low",
            "low": "Low",
            "normal": "Normal",
            "high": "High",
            "very_high": "Very High",
        }[self.status]

    @property
    def display_title(self) -> str:
        return f"{self.value} {self.trend_arrow}"

    @property
    def age_minutes(self) -> int:
        delta = datetime.now(timezone.utc) - self.timestamp
        return int(delta.total_seconds() / 60)

    @property
    def age_text(self) -> str:
        mins = self.age_minutes
        if mins < 1:
            return "just now"
        elif mins == 1:
            return "1 min ago"
        else:
            return f"{mins} min ago"

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "trend": self.trend_arrow,
            "trend_desc": self.trend_description,
            "status": self.status,
            "status_text": self.status_text,
            "color": self.color_hex,
            "age_min": self.age_minutes,
            "age_text": self.age_text,
            "timestamp": int(self.timestamp.timestamp()),
        }
