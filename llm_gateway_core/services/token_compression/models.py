"""RTK Token Compression data models."""

from dataclasses import dataclass, field


@dataclass
class CompressionStats:
    input_bytes: int = 0
    output_bytes: int = 0
    filters_applied: list[str] = field(default_factory=list)
    hits: int = 0

    @property
    def saved_bytes(self) -> int:
        return self.input_bytes - self.output_bytes

    @property
    def saved_pct(self) -> float:
        if self.input_bytes == 0:
            return 0.0
        return (self.saved_bytes / self.input_bytes) * 100.0
