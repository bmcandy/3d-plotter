"""Machine profile: editable machine-specific values."""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel


class MachineProfile(BaseModel):
    bed_width: float = 300.0
    bed_height: float = 300.0
    pen_up_z: float = 1.5
    pen_down_z: float = 0.0
    travel_feed: float = 3000.0
    draw_feed: float = 1500.0
    # homing/initialization commands, run after the automatic pen lift;
    # one G-code command per line
    init_gcode: str = "G28 X Y\nG28 Z"


class ProfileStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._profile = MachineProfile()
        if self.path.exists():
            self._profile = MachineProfile(**json.loads(self.path.read_text()))

    def get(self) -> MachineProfile:
        return self._profile

    def set(self, profile: MachineProfile) -> MachineProfile:
        self._profile = profile
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(profile.model_dump_json(indent=2))
        return self._profile
