"""Which pull-request files change data, so /handle stops for a human.

Migrations and commands/scripts are flagged on any change. Translations are
flagged unless the change only adds lines: new keys are tolerated, editing
or removing existing ones is not. In a JSON catalog, the entry that gains a
trailing comma when a key is appended after it does not count as edited.
"""

from fnmatch import fnmatchcase
from typing import Dict, List

_LABELS = {
    "migrations": "migration",
    "scripts": "command/script",
    "translations": "translation change",
}


def flagged_files(files: List[dict], guard: Dict[str, List[str]]) -> List[dict]:
    """The files that need a human: ``{filename, sha, label}``, in PR order.

    A renamed file is matched on its old path too: moving a migration out of
    its folder is still a change to it.
    """
    flagged = []
    for f in files:
        paths = [f["filename"], f.get("previous_filename") or ""]
        category = next(filter(None, (_category(p, guard) for p in paths if p)), None)
        if category is None:
            continue
        if category == "translations" and _only_adds(f):
            continue
        flagged.append({"filename": f["filename"], "sha": f.get("sha") or "",
                        "label": _LABELS[category]})
    return flagged


def truncated_listing(listed: int, total: int, head_sha: str) -> List[dict]:
    """A stand-in entry when GitHub listed fewer files than the PR changes.

    The unlisted files cannot be checked, so a human approves this head.
    """
    if listed >= total:
        return []
    return [{"filename": f"{total - listed} of {total} files not listed by GitHub",
             "sha": head_sha, "label": "unchecked"}]


def approval_key(f: dict) -> str:
    """A file approved at this content; a later edit needs a new approval."""
    return f"{f['filename']}@{f['sha']}"


def _category(path: str, guard: Dict[str, List[str]]):
    for category in _LABELS:
        if any(fnmatchcase(path, pattern) for pattern in guard.get(category, ())):
            return category
    return None


def _only_adds(f: dict) -> bool:
    if f.get("status") == "added":
        return True
    if f.get("status") != "modified" or not f.get("additions"):
        # A modified file with no added line is a binary catalog or a mode
        # change: its content cannot be read from the line counts.
        return False
    return not f.get("deletions") or _deletions_only_gain_a_comma(f)


def _deletions_only_gain_a_comma(f: dict) -> bool:
    """True for a JSON catalog whose deleted lines all come back with a trailing comma.

    Appending a key after the last entry rewrites that entry's line this way.
    Without a patch (GitHub omits large ones) the change cannot be read.
    """
    patch = f.get("patch") or ""
    if not f["filename"].endswith(".json") or not patch:
        return False
    lines = patch.splitlines()
    added = {line[1:].rstrip() for line in lines if line.startswith("+")}
    removed = [line[1:].rstrip() for line in lines if line.startswith("-")]
    return len(removed) == f["deletions"] and all(f"{line}," in added for line in removed)
