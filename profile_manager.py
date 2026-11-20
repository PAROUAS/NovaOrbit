from __future__ import annotations

import json
import re
from pathlib import Path

PROFILE_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


def load_selected_profile(registry: Path, selection_file: Path) -> dict[str, str]:
    try:
        selection = json.loads(selection_file.read_text(encoding="utf-8"))
        payload = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read the NOVA profile selection: {error}") from error

    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or not isinstance(payload.get("profiles"), list)
        or not isinstance(selection, dict)
    ):
        raise RuntimeError("The NOVA profile registry or selection is invalid.")

    profile_id = selection.get("id")
    profile_name = selection.get("name")
    if not isinstance(profile_id, str) or not PROFILE_ID_PATTERN.fullmatch(profile_id):
        raise RuntimeError("The selected NOVA profile identifier is invalid.")
    for profile in payload["profiles"]:
        if not isinstance(profile, dict):
            raise RuntimeError("The NOVA profile registry contains an invalid entry.")
        if profile.get("id") != profile_id:
            continue
        if profile.get("name") != profile_name:
            raise RuntimeError("The selected NOVA profile does not match its saved identity.")
        if not isinstance(profile_name, str) or not profile_name.strip():
            raise RuntimeError("The selected NOVA profile name is invalid.")
        return {"id": profile_id, "name": profile_name}
    raise RuntimeError("The selected NOVA profile is not present in the saved profile list.")
