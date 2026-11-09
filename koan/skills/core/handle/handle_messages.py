"""Fixed comment templates for the /handle pipeline.

The pipeline's status comments are deterministic (no model call per tick), so
they are templated here per reply language. The language follows the
instance's ``/language`` preference; unknown languages fall back to English.
"""

from app.language_preference import get_language

_TEMPLATES = {
    "english": {
        "triage_go": "Triage done — I'm taking this issue: planning next.\n\n{summary}",
        "triage_needs_human": (
            "@{requester} I need a human before going further.\n\n{summary}\n\n"
            "{questions}\nAnswer in this thread, then comment `@{bot} handle` to resume."
        ),
        "triage_not_a_bug": (
            "@{requester} I don't think this needs a code change.\n\n{summary}\n\n"
            "If I'm wrong, explain why in this thread and comment `@{bot} handle`."
        ),
        "plan_questions": (
            "@{requester} the plan above has open questions. Answer them in this thread, "
            "then comment `@{bot} handle`: I'll revise the plan and continue."
        ),
        "plan_missing": (
            "@{requester} I couldn't find a fresh plan with an *Open Questions* section. "
            "Comment `@{bot} handle` to plan again."
        ),
        "implement_no_pr": (
            "@{requester} the implementation did not open a pull request. "
            "Comment `@{bot} handle` to try again."
        ),
        "ci_blocked": (
            "@{requester} CI on {pr_url} is stuck ({outcome}) and my fixes ran out. "
            "Take a look, then comment `@{bot} handle` to resume."
        ),
        "review_exhausted": (
            "@{requester} {pr_url} still has blocking review findings after {rounds} "
            "review/fix rounds. Take a look, then comment `@{bot} handle` for one more round."
        ),
        "review_missing": (
            "@{requester} the review of {pr_url} produced no verdict. "
            "Comment `@{bot} handle` to review again."
        ),
        "done": (
            "@{requester} {pr_url} is ready for review: CI is green and my last review "
            "has no blocking finding.{qa_note}"
        ),
        "qa_ran": " Browser QA ran against the preview — see its comment.",
        "qa_no_report": " Browser QA was queued but left no report (failed or timed out).",
        "qa_preview_failed": " Browser QA was skipped: the preview deployment failed.",
        "qa_preview_timeout": " Browser QA was skipped: the preview was not ready in time.",
        "stopped": "{pr_url} was {state} — I stop handling this issue.",
        "busy": "Already on it — current step: {phase}.",
        "not_tracked": "No pipeline found for this pull request; run `@{bot} handle` on its issue.",
    },
    "french": {
        "triage_go": "Tri effectué — je prends cette issue : je passe au plan.\n\n{summary}",
        "triage_needs_human": (
            "@{requester} j'ai besoin d'un humain avant d'aller plus loin.\n\n{summary}\n\n"
            "{questions}\nRépondez dans ce fil, puis commentez `@{bot} handle` pour reprendre."
        ),
        "triage_not_a_bug": (
            "@{requester} je ne pense pas qu'un changement de code soit nécessaire.\n\n"
            "{summary}\n\nSi je me trompe, expliquez pourquoi dans ce fil et commentez "
            "`@{bot} handle`."
        ),
        "plan_questions": (
            "@{requester} le plan ci-dessus contient des questions ouvertes. Répondez-y dans "
            "ce fil, puis commentez `@{bot} handle` : je réviserai le plan et je continuerai."
        ),
        "plan_missing": (
            "@{requester} je ne trouve pas de plan récent avec une section *Open Questions*. "
            "Commentez `@{bot} handle` pour relancer le plan."
        ),
        "implement_no_pr": (
            "@{requester} l'implémentation n'a pas ouvert de pull request. "
            "Commentez `@{bot} handle` pour réessayer."
        ),
        "ci_blocked": (
            "@{requester} la CI de {pr_url} est bloquée ({outcome}) et mes corrections sont "
            "épuisées. Jetez un œil, puis commentez `@{bot} handle` pour reprendre."
        ),
        "review_exhausted": (
            "@{requester} {pr_url} a encore des remarques bloquantes après {rounds} tours de "
            "revue et de correction. Jetez un œil, puis commentez `@{bot} handle` pour un tour de plus."
        ),
        "review_missing": (
            "@{requester} la revue de {pr_url} n'a pas produit de verdict. "
            "Commentez `@{bot} handle` pour relancer la revue."
        ),
        "done": (
            "@{requester} {pr_url} est prête pour la revue : la CI est verte et ma dernière "
            "revue n'a aucune remarque bloquante.{qa_note}"
        ),
        "qa_ran": " La QA navigateur a tourné sur la preview — voir son commentaire.",
        "qa_no_report": " La QA navigateur a été lancée mais n'a laissé aucun rapport (échec ou timeout).",
        "qa_preview_failed": " QA navigateur non lancée : le déploiement de la preview a échoué.",
        "qa_preview_timeout": " QA navigateur non lancée : la preview n'était pas prête à temps.",
        "stopped": "{pr_url} a été {state} — j'arrête de traiter cette issue.",
        "busy": "Déjà en cours — étape actuelle : {phase}.",
        "not_tracked": (
            "Aucun suivi pour cette pull request ; lancez `@{bot} handle` sur son issue."
        ),
    },
}


def render(key: str, **values: str) -> str:
    """Render template *key* in the configured reply language."""
    language = (get_language() or "english").strip().lower()
    templates = _TEMPLATES.get(language, _TEMPLATES["english"])
    return templates[key].format(**values)
