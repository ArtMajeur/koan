---
name: handle
scope: core
group: code
emoji: 🧭
description: "Carry a GitHub issue end to end: triage, plan, implement, CI, review/fix loop, QA, hand-off"
version: 1.0.0
audience: hybrid
caveman: false
github_enabled: true
github_context_aware: false
commands:
  - name: handle
    description: "Take a GitHub issue from triage to a review-ready PR, stopping at human gates"
    usage: "/handle <issue-url>"
handler: handler.py
---
