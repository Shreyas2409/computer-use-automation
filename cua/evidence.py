"""Evidence writer: structured logging + screenshot capture with redaction.

Every disk write passes through redaction to keep PII/secret-sensitivity values out of evidence.
Logs a value's SHAPE, not the value itself: member_id=<redacted:string:5>
"""

from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime
from dataclasses import asdict, dataclass
from typing import Any

from cua.policy import redact_sensitive_value
from cua.schema import Capability, ParamSpec, Sensitivity


@dataclass
class EvidenceLog:
    """Structured log entry for evidence capture."""
    timestamp: str
    phase: str  # "discovery", "replay", "intervention"
    step_index: int | None = None
    action: str | None = None
    message: str | None = None
    error: str | None = None
    metadata: dict[str, Any] | None = None


def _redact_metadata(
    metadata: dict[str, Any] | None,
    params: dict[str, "ParamSpec"] | None,
) -> dict[str, Any]:
    """Redact metadata: sensitivity-driven first, then regex fallback."""
    out: dict[str, Any] = {}
    if not metadata:
        return out
    for key, val in metadata.items():
        if params and key in params and val is not None:
            out[key] = EvidenceWriter.redact_param_value(params[key], val)
        elif isinstance(val, str):
            out[key] = redact_sensitive_value(val)
        else:
            out[key] = val
    return out


class EvidenceWriter:
    """Write evidence (logs, screenshots) with automatic redaction."""
    
    def __init__(self, evidence_dir: Path | str):
        """Initialize evidence writer.
        
        Args:
            evidence_dir: Base directory for evidence output
        """
        self.evidence_dir = Path(evidence_dir)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.logs: list[EvidenceLog] = []
    
    def log(
        self,
        phase: str,
        action: str | None = None,
        step_index: int | None = None,
        message: str | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
        params: dict[str, ParamSpec] | None = None,
    ) -> None:
        """Add a log entry, with redaction applied to all sensitive fields.

        `params` maps metadata keys to their ParamSpec. Values for those keys
        are redacted by declared sensitivity (shape-only for pii/secret) BEFORE
        the regex fallback runs, so pii/secret values never reach disk raw.
        """
        redacted_metadata = _redact_metadata(metadata, params)

        entry = EvidenceLog(
            timestamp=datetime.utcnow().isoformat(),
            phase=phase,
            action=action,
            step_index=step_index,
            message=message,
            error=error,
            metadata=redacted_metadata or None,
        )
        self.logs.append(entry)

    def write_logs(self, filename: str = "evidence.json") -> Path:
        """Write all logs to a JSON file with redaction applied.

        Any metadata values already redacted at log() time by ParamSpec
        sensitivity remain in their shape-only form here; regex-based
        redaction still runs over the free-text message/error fields.
        """
        output_path = self.evidence_dir / filename

        log_dicts = []
        for log in self.logs:
            log_dict = asdict(log)
            if log_dict.get("message"):
                log_dict["message"] = redact_sensitive_value(log_dict["message"])
            if log_dict.get("error"):
                log_dict["error"] = redact_sensitive_value(log_dict["error"])
            if log_dict.get("metadata"):
                redacted_meta = {}
                for key, val in log_dict["metadata"].items():
                    if isinstance(val, str):
                        redacted_meta[key] = redact_sensitive_value(val)
                    else:
                        redacted_meta[key] = val
                log_dict["metadata"] = redacted_meta
            log_dicts.append(log_dict)

        output_path.write_text(json.dumps(log_dicts, indent=2))
        return output_path
    
    def write_screenshot(self, screenshot_bytes: bytes, name: str = "screenshot.png") -> Path:
        """Write screenshot bytes to evidence directory.
        
        No redaction needed for binary; metadata is handled separately.
        """
        output_path = self.evidence_dir / name
        output_path.write_bytes(screenshot_bytes)
        self.log(
            phase="evidence",
            message=f"Screenshot saved: {name}",
            metadata={"size_bytes": len(screenshot_bytes)},
        )
        return output_path
    
    @staticmethod
    def redact_param_value(param: ParamSpec, value: Any) -> str:
        """Redact a parameter value based on its sensitivity level.
        
        Returns a redaction token like <redacted:pii:string:5> or the value itself.
        """
        if param.sensitivity in ("secret", "pii"):
            # Always redact
            val_str = str(value)
            val_type = param.type
            val_len = len(val_str)
            return f"<redacted:{param.sensitivity}:{val_type}:{val_len}>"
        elif param.sensitivity == "internal":
            # Never include in evidence
            return "<redacted:internal>"
        else:
            # "public" can be logged
            return str(value)
