"""Fixed comment templates for the /handle pipeline.

The pipeline's status comments are deterministic (no model call per tick), so
they are templated here per reply language. The language follows the
instance's ``/language`` preference; unknown languages fall back to English.
``{mention}`` is ``"@login: "`` or empty when no authorized requester is known.
"""

from app.language_preference import get_language

_TEMPLATES = {
    "english": {
        "triage_go": "Triage done — I'm taking this issue: planning next.\n\n{summary}",
        "triage_needs_human": (
            "{mention}I need a human before going further.\n\n{summary}\n\n"
            "{questions}\nAnswer in this thread, then comment `@{bot} handle` to resume."
        ),
        "triage_not_a_bug": (
            "{mention}I don't think this needs a code change.\n\n{summary}\n\n"
            "If I'm wrong, explain why in this thread and comment `@{bot} handle`."
        ),
        "triage_failed": "The triage produced no usable verdict.",
        "plan_questions": (
            "{mention}The plan above has open questions. Answer them in this thread, "
            "then comment `@{bot} handle`: I'll revise the plan and continue."
        ),
        "plan_missing": (
            "{mention}I couldn't find a fresh plan with an *Open Questions* section. "
            "Comment `@{bot} handle` to plan again."
        ),
        "implement_no_pr": (
            "{mention}The implementation did not open or update a pull request. "
            "Comment `@{bot} handle` to try again."
        ),
        "ci_blocked": (
            "{mention}CI on {pr_url} is stuck (`{outcome}`) and I can't get it further "
            "on my own. Take a look, then comment `@{bot} handle` to resume."
        ),
        "review_exhausted": (
            "{mention}{pr_url} still has blocking review findings after {rounds} "
            "fix rounds. Take a look, then comment `@{bot} handle` for one more round."
        ),
        "review_missing": (
            "{mention}The review of {pr_url} produced no verdict for its latest commit. "
            "Comment `@{bot} handle` to review again."
        ),
        "done": (
            "{mention}{pr_url} is ready for review: {ci_note}, and my last review "
            "has no blocking finding.{qa_note}"
        ),
        "ci_green": "CI is green",
        "ci_none": "no CI ran on its latest commit",
        "ci_other": "CI on its latest commit is {state}",
        "qa_ran": " Browser QA ran against the preview — see its comment.",
        "qa_no_report": " Browser QA was queued but left no report (failed or timed out).",
        "qa_preview_failed": " Browser QA was skipped: the preview deployment failed.",
        "qa_preview_timeout": " Browser QA was skipped: the preview was not ready in time.",
        "board_move_failed": (
            "{mention}I couldn't move this issue to **{column}** on the project board. "
            "Move it by hand. GitHub said: `{error}`"
        ),
        "new_round": "{mention}Starting a new fix round on {pr_url} from the latest review.",
        "stopped": "{pr_url} was {state} — I stop handling this issue.",
        "merged": "{pr_url} is merged — nothing left to do.",
        "issue_closed": (
            "This issue is closed — I stop handling it. Reopen it and comment "
            "`@{bot} handle` to pick it up again."
        ),
        "pr_closed": (
            "This pull request is closed — I stop handling it. Reopen it and comment "
            "`@{bot} handle` to pick it up again."
        ),
        "busy": "Already on it — current step: {phase}.",
        "gate_open": (
            "{mention}I'm still waiting for a reply to the question above. Answer in this "
            "thread or on the pull request, then comment `@{bot} handle`."
        ),
        "error": (
            "{mention}I hit an error at step `{phase}`: {error}\n\n"
            "Comment `@{bot} handle` to retry that step."
        ),
        "fork_pr": (
            "{mention}This pull request comes from a fork: I can't push fixes to its "
            "branch, so I won't handle it."
        ),
        "state_closed": "closed",
        "state_merged": "merged",
    },
    "french": {
        "triage_go": "Tri effectué — je prends cette issue : je passe au plan.\n\n{summary}",
        "triage_needs_human": (
            "{mention}J'ai besoin d'un humain avant d'aller plus loin.\n\n{summary}\n\n"
            "{questions}\nRépondez dans ce fil, puis commentez `@{bot} handle` pour reprendre."
        ),
        "triage_not_a_bug": (
            "{mention}Je ne pense pas qu'un changement de code soit nécessaire.\n\n"
            "{summary}\n\nSi je me trompe, expliquez pourquoi dans ce fil et commentez "
            "`@{bot} handle`."
        ),
        "triage_failed": "Le tri n'a pas produit de verdict exploitable.",
        "plan_questions": (
            "{mention}Le plan ci-dessus contient des questions ouvertes. Répondez-y dans "
            "ce fil, puis commentez `@{bot} handle` : je réviserai le plan et je continuerai."
        ),
        "plan_missing": (
            "{mention}Je ne trouve pas de plan récent avec une section *Open Questions*. "
            "Commentez `@{bot} handle` pour relancer le plan."
        ),
        "implement_no_pr": (
            "{mention}L'implémentation n'a ni ouvert ni mis à jour de pull request. "
            "Commentez `@{bot} handle` pour réessayer."
        ),
        "ci_blocked": (
            "{mention}La CI de {pr_url} est bloquée (`{outcome}`) et je ne peux pas aller "
            "plus loin seul. Jetez un œil, puis commentez `@{bot} handle` pour reprendre."
        ),
        "review_exhausted": (
            "{mention}{pr_url} a encore des remarques bloquantes après {rounds} tours de "
            "correction. Jetez un œil, puis commentez `@{bot} handle` pour un tour de plus."
        ),
        "review_missing": (
            "{mention}La revue de {pr_url} n'a pas produit de verdict pour son dernier "
            "commit. Commentez `@{bot} handle` pour relancer la revue."
        ),
        "done": (
            "{mention}{pr_url} est prête pour la revue : {ci_note}, et ma dernière "
            "revue n'a aucune remarque bloquante.{qa_note}"
        ),
        "ci_green": "la CI est verte",
        "ci_none": "aucune CI n'a tourné sur son dernier commit",
        "ci_other": "la CI de son dernier commit est en état {state}",
        "qa_ran": " La QA navigateur a tourné sur la preview — voir son commentaire.",
        "qa_no_report": (
            " La QA navigateur a été lancée mais n'a laissé aucun rapport (échec ou timeout)."
        ),
        "qa_preview_failed": " QA navigateur non lancée : le déploiement de la preview a échoué.",
        "qa_preview_timeout": " QA navigateur non lancée : la preview n'était pas prête à temps.",
        "board_move_failed": (
            "{mention}Je n'ai pas pu déplacer cette issue dans **{column}** sur le tableau "
            "du projet. Déplacez-la à la main. GitHub a répondu : `{error}`"
        ),
        "new_round": (
            "{mention}Je lance un nouveau tour de correction sur {pr_url} "
            "à partir de la dernière revue."
        ),
        "stopped": "{pr_url} a été {state} — j'arrête de traiter cette issue.",
        "merged": "{pr_url} est fusionnée — plus rien à faire.",
        "issue_closed": (
            "Cette issue est fermée — j'arrête de la traiter. Rouvrez-la et commentez "
            "`@{bot} handle` pour reprendre."
        ),
        "pr_closed": (
            "Cette pull request est fermée — j'arrête de la traiter. Rouvrez-la et "
            "commentez `@{bot} handle` pour reprendre."
        ),
        "busy": "Déjà en cours — étape actuelle : {phase}.",
        "gate_open": (
            "{mention}J'attends encore une réponse à la question ci-dessus. Répondez dans ce "
            "fil ou sur la pull request, puis commentez `@{bot} handle`."
        ),
        "error": (
            "{mention}J'ai rencontré une erreur à l'étape `{phase}` : {error}\n\n"
            "Commentez `@{bot} handle` pour relancer cette étape."
        ),
        "fork_pr": (
            "{mention}Cette pull request vient d'un fork : je ne peux pas pousser de "
            "correctifs sur sa branche, donc je ne la traite pas."
        ),
        "state_closed": "fermée",
        "state_merged": "fusionnée",
    },
}


def render(key: str, **values: str) -> str:
    """Render template *key* in the configured reply language."""
    language = (get_language() or "english").strip().lower()
    templates = _TEMPLATES.get(language, _TEMPLATES["english"])
    return templates[key].format(**values)
