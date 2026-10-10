"""Audit log: one JSON line per decision. Done for you; extend the fields if your policy needs more."""
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

# gate.py judge/try set GATE_LOG_DIR so evaluation runs never pollute your demo audit sample.
LOG = Path(os.getenv("GATE_LOG_DIR") or Path(__file__).parent) / "audit_log.jsonl"
_lock = threading.Lock()


def record(persona, text, context, decision):
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "persona": persona,
        "input": text,
        "context": context,
        "decision": decision.decision,
        "reason": decision.reason,
        "evidence": decision.evidence,
        "findings": getattr(decision, "findings", []),
        "signals": getattr(decision, "signals", {}),
        "ezio": decision.ezio,
    }
    with _lock, LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")


REVIEWS = LOG.parent / "reviews.jsonl"


def record_review(persona, text, outcome, note):
    """A human's review of a held input. Kept apart from audit_log.jsonl so every decision entry stays unchanged."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "persona": persona,
        "input": text,
        "decision": "HOLD_FOR_REVIEW",
        "outcome": outcome,
        "note": note,
    }
    with _lock, REVIEWS.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _tail(path, limit):
    if not path.exists():
        return []
    entries = []
    with _lock:
        lines = path.read_text().splitlines()
    for line in lines[-limit:]:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(entries))


def recent(limit=50):
    """Newest-first decisions and reviews for the page's audit view (decision fields without the full Ezio response)."""
    decisions = [{k: e.get(k) for k in ("timestamp", "persona", "input", "decision", "reason", "context")}
                 for e in _tail(LOG, limit)]
    reviews = _tail(REVIEWS, limit)
    return {"decisions": decisions, "reviews": reviews}
