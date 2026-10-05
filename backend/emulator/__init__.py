"""Station emulator package. This module is independent of backend.model."""
from .world import EMULATOR_VERSION, snapshot

__all__ = ["EMULATOR_VERSION", "snapshot"]
