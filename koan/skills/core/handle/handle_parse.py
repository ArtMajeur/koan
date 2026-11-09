"""Pure parsers for the /handle pipeline: triage verdict, plan gate, review verdict."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

VERDICTS = ("go", "needs_human", "not_a_bug")

_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_COMMENT_FOOTER = "\n---\n"
# The whole section must be one "none" line, optionally followed by "ready to
# implement" and nothing else: "None — ready to implement.", "No open
# questions.", "N/A". "None of these block, but: …", "N/A — but please confirm
# …" and "None: <anything>" are questions.
_NO_QUESTIONS_RE = re.compile(
    r"^[\s*_>•-]*"
    r"(?:none|n/?a|no(?:\s+open)?\s+questions?|aucune?(?:\s+questions?(?:\s+ouvertes?)?)?)"
    r"[\s*_.]*"
    r"(?:[—–:-]\s*(?:ready\s+to\s+implement|prête?\s+à\s+implémenter)[\s*_.!]*)?$",
    re.IGNORECASE,
)


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


def _plan_text(comment_body: str) -> str:
    """Drop the comment footer that /plan appends after a horizontal rule."""
    body = comment_body or ""
    if _COMMENT_FOOTER in body:
        body = body.rsplit(_COMMENT_FOOTER, 1)[0]
    return body


def open_questions(comment_body: str) -> Optional[str]:
    """Return the plan's open questions: "" when none, None when no such section.

    Section boundaries come from ``plan_runner`` (the writer of the section),
    so an assumptions audit folded into it counts as a question.
    """
    from app.plan_runner import extract_open_questions

    section = extract_open_questions(_plan_text(comment_body))
    if section is None:
        return None
    lines = [line for line in section.splitlines() if line.strip()]
    if not lines or (len(lines) == 1 and _NO_QUESTIONS_RE.match(lines[0])):
        return ""
    return section


def has_plan(comment_body: str) -> bool:
    """True when a comment carries a plan's open-questions section."""
    from app.plan_runner import extract_open_questions

    return extract_open_questions(_plan_text(comment_body)) is not None


def review_verdict(sidecar_path: Path, since: float, head_sha: str) -> Optional[bool]:
    """Return the review's ``lgtm`` when it reviewed *head_sha* at/after *since*.

    None means no fresh, well-formed verdict for the current head (missing
    file, stale review, another commit, or no boolean ``lgtm``).
    """
    try:
        data = json.loads(sidecar_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("head_sha") != head_sha:
        return None
    try:
        written = datetime.strptime(data.get("timestamp", ""), "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
    if written.replace(tzinfo=timezone.utc).timestamp() < since:
        return None
    lgtm = (data.get("review_summary") or {}).get("lgtm")
    return lgtm if isinstance(lgtm, bool) else None
