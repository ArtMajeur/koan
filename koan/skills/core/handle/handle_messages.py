"""Fixed comment templates for the /handle pipeline.

The pipeline's status comments are deterministic (no model call per tick), so
they are templated here per reply language. The language follows the
instance's ``/language`` preference; unknown languages fall back to English.
``{mention}`` is ``"@login: "`` or empty when no authorized requester is known.
"""

import re

from app.language_preference import get_language

# Closes every pipeline comment with its record key. Comments move with a
# transferred issue, so the marker is how a tick finds the record the issue
# had under its old URL. Anchored to the end: text quoted inside a comment
# (a triage summary) is never read as a marker.
_MARKER_RE = re.compile(r"\n<!-- koan-handle: (\S+) -->\s*\Z")


def with_marker(body: str, record_key: str) -> str:
    return f"{body}\n\n<!-- koan-handle: {record_key} -->"


def marked_key(body: str) -> str:
    """The record key a pipeline comment closes with ("" when it has none)."""
    match = _MARKER_RE.search(body or "")
    return match.group(1) if match else ""

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
        "data_change": (
            "{mention}{pr_url} changes data, and I don't ship that without a human:\n\n"
            "{files}\n\nCheck these files (or take over the change). Comment "
            "`@{bot} handle` to approve them as they are now and continue; any later "
            "edit to them will stop me again."
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
        "pr_adopted": (
            "{mention}{pr_url} already closes this issue: I take it through CI and review "
            "instead of starting over."
        ),
        "prs_status": (
            "{mention}This issue is closed by pull requests I handle one by one, each on "
            "its own thread:\n\n{prs}\n\nComment `@{bot} handle` on a pull request to move "
            "it. Here, it gives this status and wakes the ones waiting for an answer; a "
            "data change is only approved on its own pull request."
        ),
        "prs_done": "{mention}Every pull request for this issue is ready for review:\n\n{prs}",
        "prs_owned": (
            "{mention}The pull requests that close this issue are already handled from "
            "another issue, so I don't take this one:\n\n{prs}"
        ),
        "pr_state_new": "starting (CI, then review)",
        "pr_state_running": "in progress (step: {phase})",
        "pr_state_gate": "waiting for a human, see its thread",
        "pr_state_data_gate": "waiting for a human to approve a data change, on its thread",
        "pr_state_unauthorized": (
            "not handled: you are not allowed to command me on its repository"
        ),
        "prs_merged": "The pull requests of this issue are merged — nothing left to do:\n\n{prs}",
        "pr_state_owned": "handled by the pipeline of {issue_url}, which it also closes",
        "pr_state_lost": "can no longer be read: I stop waiting for it",
        "prs_closed": (
            "{mention}The pull requests of this issue were closed without being merged, "
            "so I stop here:\n\n{prs}\n\nComment `@{bot} handle` to start over from the issue."
        ),
        "pr_state_done": "ready for review",
        "pr_state_stopped": "closed or merged",
        "pr_state_fork": "not handled: it comes from a fork",
        "pr_state_no_project": "not handled: no project is configured for its repository",
        "follow_ups_proposed": (
            "{mention}Here is what I would open in `{repo}`. Nothing exists yet.\n\n"
            "{drafts}\n\nComment `@{bot} handle confirm` to open them exactly as "
            "written. To change them, ask again in other words: a new draft replaces "
            "this one."
        ),
        "nothing_to_confirm": (
            "{mention}There is no draft of yours waiting for a confirmation here (it was "
            "opened already, replaced by a newer one, or is somebody else's). `confirm` "
            "does nothing else: to move this pipeline, comment `@{bot} handle`."
        ),
        "follow_ups_created": "{mention}As confirmed, I opened:\n\n{issues}",
        "follow_ups_failed": (
            "{mention}I couldn't open the follow-up issue you confirmed: `{error}`. "
            "What I did open is listed above, if anything; open the rest by hand. "
            "I carry on with this one."
        ),
        "follow_up_footer": "Follow-up of {issue_url}, opened at the request of {login}.",
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
        "data_change": (
            "{mention}{pr_url} modifie des données, et je ne livre pas ça sans un humain :\n\n"
            "{files}\n\nVérifiez ces fichiers (ou reprenez la main). Commentez "
            "`@{bot} handle` pour les approuver dans leur état actuel et continuer ; toute "
            "modification ultérieure de ces fichiers m'arrêtera à nouveau."
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
        "pr_adopted": (
            "{mention}{pr_url} ferme déjà cette issue : je l'emmène en CI et en revue "
            "au lieu de repartir de zéro."
        ),
        "prs_status": (
            "{mention}Cette issue est fermée par des pull requests que je traite une par "
            "une, chacune dans son fil :\n\n{prs}\n\nCommentez `@{bot} handle` sur une pull "
            "request pour la faire avancer. Ici, il donne cet état et relance celles qui "
            "attendent une réponse ; une modification de données ne s'approuve que sur sa "
            "propre pull request."
        ),
        "prs_done": (
            "{mention}Toutes les pull requests de cette issue sont prêtes pour la revue :"
            "\n\n{prs}"
        ),
        "prs_owned": (
            "{mention}Les pull requests qui ferment cette issue sont déjà traitées depuis "
            "une autre issue, donc je ne prends pas celle-ci :\n\n{prs}"
        ),
        "pr_state_new": "démarrage (CI, puis revue)",
        "pr_state_running": "en cours (étape : {phase})",
        "pr_state_gate": "en attente d'un humain, voir son fil",
        "pr_state_data_gate": (
            "en attente d'un humain pour approuver une modification de données, dans son fil"
        ),
        "pr_state_unauthorized": (
            "non traitée : vous n'êtes pas autorisé à me commander sur son dépôt"
        ),
        "prs_merged": (
            "Les pull requests de cette issue sont fusionnées — plus rien à faire :\n\n{prs}"
        ),
        "pr_state_owned": "traitée par le pipeline de {issue_url}, qu'elle ferme aussi",
        "pr_state_lost": "n'est plus lisible : j'arrête de l'attendre",
        "prs_closed": (
            "{mention}Les pull requests de cette issue ont été fermées sans être fusionnées, "
            "donc je m'arrête ici :\n\n{prs}\n\nCommentez `@{bot} handle` pour repartir de "
            "l'issue."
        ),
        "pr_state_done": "prête pour la revue",
        "pr_state_stopped": "fermée ou fusionnée",
        "pr_state_fork": "non traitée : elle vient d'un fork",
        "pr_state_no_project": "non traitée : aucun projet n'est configuré pour son dépôt",
        "follow_ups_proposed": (
            "{mention}Voici ce que j'ouvrirais dans `{repo}`. Rien n'existe encore.\n\n"
            "{drafts}\n\nCommentez `@{bot} handle confirm` pour les ouvrir tels quels. "
            "Pour les modifier, redemandez autrement : un nouveau brouillon remplace "
            "celui-ci."
        ),
        "nothing_to_confirm": (
            "{mention}Aucun brouillon à vous n'attend de confirmation ici (il a déjà été "
            "ouvert, remplacé par un plus récent, ou il est à quelqu'un d'autre). `confirm` "
            "ne fait rien d'autre : pour faire avancer ce pipeline, commentez `@{bot} handle`."
        ),
        "follow_ups_created": "{mention}Comme confirmé, j'ai ouvert :\n\n{issues}",
        "follow_ups_failed": (
            "{mention}Je n'ai pas pu ouvrir l'issue de suivi confirmée : `{error}`. "
            "Ce que j'ai ouvert est listé ci-dessus, le cas échéant ; ouvrez le reste à "
            "la main. Je continue sur celle-ci."
        ),
        "follow_up_footer": "Suite de {issue_url}, ouverte à la demande de {login}.",
        "state_closed": "fermée",
        "state_merged": "fusionnée",
    },
}


def render(key: str, **values: str) -> str:
    """Render template *key* in the configured reply language."""
    language = (get_language() or "english").strip().lower()
    templates = _TEMPLATES.get(language, _TEMPLATES["english"])
    return templates[key].format(**values)
