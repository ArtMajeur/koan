"""Pure parsers for the /handle pipeline: triage verdict, plan gate, review verdict."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

VERDICTS = ("go", "needs_human", "not_a_bug")

_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_OPEN_QUESTIONS_RE = re.compile(
    r"^#{2,4}\s+(?:open questions|questions ouvertes)\s*$", re.IGNORECASE | re.MULTILINE,
)
_SECTION_END_RE = re.compile(r"^(?:#{1,4}\s|-{3,}\s*$)", re.MULTILINE)
_NO_QUESTIONS_RE = re.compile(r"^[\W_]*(?:none|aucune?|rien|n/?a)\b", re.IGNORECASE)


def parse_triage(output: str) -> Optional[dict]:
    """Return ``{verdict, summary, questions}`` from the triage output, or None.

    Takes the last fenced ``json`` block; rejects unknown verdicts and a
    missing summary so a malformed answer is gated, never acted on.
    """
    blocks = _JSON_BLOCK_RE.findall(output or "")
    if not blocks:
        return None
    try:
        data = json.loads(blocks[-1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("verdict") not in VERDICTS:
        return None
    summary = str(data.get("summary") or "").strip()
    if not summary:
        return None
    questions = data.get("questions") or []
    if not isinstance(questions, list):
        questions = [str(questions)]
    return {
        "verdict": data["verdict"],
        "summary": summary,
        "questions": [str(q).strip() for q in questions if str(q).strip()],
    }


def open_questions(plan_body: str) -> Optional[str]:
    """Return the plan's open questions: "" when none, None when no such section.

    The section runs to the next heading or horizontal rule (the comment
    footer starts with one).
    """
    match = _OPEN_QUESTIONS_RE.search(plan_body or "")
    if not match:
        return None
    rest = plan_body[match.end():]
    end = _SECTION_END_RE.search(rest)
    section = (rest[: end.start()] if end else rest).strip()
    if not section or _NO_QUESTIONS_RE.match(section):
        return ""
    return section


def has_plan(body: str) -> bool:
    """True when a comment carries a plan's open-questions section."""
    return bool(_OPEN_QUESTIONS_RE.search(body or ""))


def review_verdict(sidecar_path: Path, since: float) -> Optional[bool]:
    """Return the review's ``lgtm`` if the sidecar was written at/after *since*.

    None means no fresh, well-formed verdict (missing file, stale review,
    or no boolean ``lgtm``).
    """
    try:
        data = json.loads(sidecar_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    try:
        written = datetime.strptime(data.get("timestamp", ""), "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
    if written.replace(tzinfo=timezone.utc).timestamp() < since:
        return None
    lgtm = (data.get("review_summary") or {}).get("lgtm")
    return lgtm if isinstance(lgtm, bool) else None
