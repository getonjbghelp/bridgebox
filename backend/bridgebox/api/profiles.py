"""Profile export/import - the copy-paste and file-based paths for carrying
profiles between machines. See desktop.py's own docstring on the mixin
split."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import webview

from ..profiles_io import export_payload, import_payload

logger = logging.getLogger(__name__)


class ProfilesMixin:
    def export_profiles(self) -> dict:
        """The user's own profiles as JSON text, for the copy-paste path."""
        try:
            payload = export_payload(self._config.profiles)
            return {
                "ok": True,
                "error": None,
                "json": json.dumps(payload, ensure_ascii=False, indent=2),
                "count": len(payload["profiles"]),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "json": "", "count": 0}

    def import_profiles(self, text: str) -> dict:
        """Add profiles from pasted JSON.

        Everything the import is allowed and not allowed to do lives in
        profiles_io.import_payload - notably that it never overwrites an
        existing profile and never changes which one is active. Here it is
        only persisted."""
        try:
            merged, report = import_payload(text, into=self._config.profiles)
            result = self.update_config({"profiles": merged.model_dump()})
            return {
                "ok": result["ok"],
                "error": result["error"],
                "config": result.get("config"),
                "report": report,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "config": None, "report": None}

    def export_profiles_to_file(self) -> dict:
        """Same export, through the native save dialog. A thin wrapper on
        purpose: the format lives in one place."""
        try:
            if self._window is None:
                return {"ok": False, "error": "окно недоступно", "path": ""}
            chosen = self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename="bridgebox-profiles.json",
                file_types=("JSON (*.json)",),
            )
            if not chosen:
                return {"ok": True, "error": None, "path": ""}  # cancelled
            path = Path(chosen[0] if isinstance(chosen, (list, tuple)) else str(chosen))
            exported = self.export_profiles()
            if not exported["ok"]:
                return {"ok": False, "error": exported["error"], "path": ""}
            path.write_text(exported["json"], encoding="utf-8")
            logger.info("exported %d profiles to %s", exported["count"], path)
            return {"ok": True, "error": None, "path": str(path)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "path": ""}

    def import_profiles_from_file(self) -> dict:
        try:
            if self._window is None:
                return {"ok": False, "error": "окно недоступно", "config": None, "report": None}
            chosen = self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False, file_types=("JSON (*.json)",)
            )
            if not chosen:
                return {"ok": True, "error": None, "config": None, "report": None}  # cancelled
            path = Path(chosen[0] if isinstance(chosen, (list, tuple)) else str(chosen))
            return self.import_profiles(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"ok": False, "error": str(exc), "config": None, "report": None}
