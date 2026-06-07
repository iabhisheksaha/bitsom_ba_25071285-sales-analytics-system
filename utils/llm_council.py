"""
llm_council.py — Andrej Karpathy llm-council pattern: 3-stage multi-model deliberation.

Stage 1: All council members answer independently in parallel.
Stage 2: Each member anonymously ranks the others' responses.
Stage 3: Chairman synthesizes using all responses + rankings.
"""

import asyncio
import json
import logging
import os
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

_DEFAULT_COUNCIL_MEMBERS: list[str] = [
    "anthropic/claude-sonnet-4-6",
    "openai/gpt-4o",
    "google/gemini-2.0-flash",
    "meta-llama/llama-3.3-70b-instruct",
]

_DEFAULT_CHAIRMAN: str = "anthropic/claude-opus-4-8"

_HTTP_TIMEOUT = 120  # seconds per request


def _load_council_config(config_path: str = "config/config.yaml") -> tuple[list[str], str]:
    """Read council members/chairman from config, falling back to defaults.

    Keeps config/config.yaml as the single source of truth so that editing
    ``council.members`` / ``council.chairman`` actually changes the council
    (previously these were ignored in favour of module constants).
    """
    try:
        import yaml
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        council = cfg.get("council", {}) or {}
        members = council.get("members") or _DEFAULT_COUNCIL_MEMBERS
        chairman = council.get("chairman") or _DEFAULT_CHAIRMAN
        return list(members), chairman
    except Exception:
        return list(_DEFAULT_COUNCIL_MEMBERS), _DEFAULT_CHAIRMAN


COUNCIL_MEMBERS, CHAIRMAN = _load_council_config()

# ---------------------------------------------------------------------------
# Internal helpers — HTTP
# ---------------------------------------------------------------------------


def _openrouter_key() -> Optional[str]:
    return os.environ.get("OPENROUTER_API_KEY")


def _anthropic_key() -> Optional[str]:
    return os.environ.get("ANTHROPIC_API_KEY")


async def _call_openrouter(
    client: httpx.AsyncClient,
    model: str,
    messages: list[dict],
    system: str,
    max_tokens: int,
) -> str:
    """Call OpenRouter chat completions endpoint. Returns the assistant text."""
    api_key = _openrouter_key()
    if not api_key:
        raise EnvironmentError("OPENROUTER_API_KEY is not set")

    payload: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if system:
        payload["messages"] = [{"role": "system", "content": system}] + messages

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/llm-council",
        "X-Title": "llm-council",
    }

    response = await client.post(
        OPENROUTER_BASE_URL,
        json=payload,
        headers=headers,
        timeout=_HTTP_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]


async def _call_anthropic_direct(
    client: httpx.AsyncClient,
    model: str,
    messages: list[dict],
    system: str,
    max_tokens: int,
) -> str:
    """Fallback: call Anthropic API directly when OpenRouter key is absent."""
    api_key = _anthropic_key()
    if not api_key:
        raise EnvironmentError(
            "Neither OPENROUTER_API_KEY nor ANTHROPIC_API_KEY is set"
        )

    # Anthropic only supports claude-* models natively; strip provider prefix
    bare_model = model.split("/")[-1] if "/" in model else model

    payload: dict = {
        "model": bare_model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        payload["system"] = system

    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }

    response = await client.post(
        ANTHROPIC_BASE_URL,
        json=payload,
        headers=headers,
        timeout=_HTTP_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    return data["content"][0]["text"]


async def _call_model(
    client: httpx.AsyncClient,
    model: str,
    messages: list[dict],
    system: str = "",
    max_tokens: int = 512,
) -> str:
    """Route to OpenRouter (preferred) or Anthropic direct (fallback)."""
    if _openrouter_key():
        return await _call_openrouter(client, model, messages, system, max_tokens)
    # Fallback: only Anthropic-family models work via direct API
    return await _call_anthropic_direct(client, model, messages, system, max_tokens)


# ---------------------------------------------------------------------------
# Stage 1 — Independent answers
# ---------------------------------------------------------------------------


async def _stage1_answers(
    client: httpx.AsyncClient,
    question: str,
    system: str,
    models: list[str],
    max_tokens: int,
) -> dict[str, str]:
    """Ask every council member independently. Returns {model: answer}."""

    async def _ask_one(model: str) -> tuple[str, Optional[str]]:
        try:
            answer = await _call_model(
                client,
                model,
                [{"role": "user", "content": question}],
                system=system,
                max_tokens=max_tokens,
            )
            return model, answer
        except Exception as exc:
            logger.warning("Stage 1 — model %s failed: %s", model, exc)
            return model, None

    tasks = [_ask_one(m) for m in models]
    results = await asyncio.gather(*tasks)
    return {model: ans for model, ans in results if ans is not None}


# ---------------------------------------------------------------------------
# Stage 2 — Peer ranking
# ---------------------------------------------------------------------------

_RANKING_SYSTEM = (
    "You are an impartial evaluator. You will be shown a question and several "
    "anonymous responses labelled Response A, Response B, etc. "
    "Rank them from best to worst based on accuracy, clarity, and helpfulness. "
    "Output your reasoning briefly, then end with:\n\n"
    "FINAL RANKING: <comma-separated list, e.g. B, A, C>\n\n"
    "Do not reveal which model wrote which response."
)


def _build_ranking_prompt(
    question: str, responses: dict[str, str], exclude: str
) -> "tuple[str, dict[str, str]]":
    """Build a prompt asking a model to rank all *other* models' responses.

    Returns ``(prompt, label_map)`` where ``label_map`` maps the anonymous
    labels (A, B, …) back to their originating model IDs.
    """
    labels = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    # Exclude the ranker's own response so it remains anonymous to itself.
    peer_items = [(m, r) for m, r in responses.items() if m != exclude]
    lines = [f"Question: {question}\n"]
    label_map: dict[str, str] = {}
    for idx, (model, response) in enumerate(peer_items):
        label = labels[idx]
        label_map[label] = model
        lines.append(f"--- Response {label} ---\n{response}\n")
    prompt = "\n".join(lines)
    return prompt, label_map


def _parse_ranking(text: str) -> list[str]:
    """Extract the ordered label list from 'FINAL RANKING: B, A, C' text."""
    match = re.search(r"FINAL RANKING\s*:\s*([A-Z,\s]+)", text, re.IGNORECASE)
    if not match:
        return []
    raw = match.group(1)
    return [tok.strip().upper() for tok in raw.split(",") if tok.strip()]


def _aggregate_rankings(
    rankings: list[dict[str, int]]
) -> dict[str, float]:
    """
    Compute average rank position for each model across all rankers.
    Lower average = higher consensus quality.

    :param rankings: List of {model: rank_position (0-based)} dicts, one per ranker.
    :returns: {model: average_rank}
    """
    totals: dict[str, list[int]] = {}
    for ranking in rankings:
        for model, pos in ranking.items():
            totals.setdefault(model, []).append(pos)
    return {
        model: sum(positions) / len(positions)
        for model, positions in totals.items()
    }


async def _stage2_rankings(
    client: httpx.AsyncClient,
    question: str,
    answers: dict[str, str],
) -> list[dict[str, int]]:
    """
    Each council member ranks the others' responses.
    Returns a list of rank dicts: [{model: position}, ...].
    """

    async def _rank_one(ranker: str) -> Optional[dict[str, int]]:
        try:
            prompt, label_map = _build_ranking_prompt(question, answers, exclude=ranker)
            text = await _call_model(
                client,
                ranker,
                [{"role": "user", "content": prompt}],
                system=_RANKING_SYSTEM,
                max_tokens=256,
            )
            labels_ordered = _parse_ranking(text)
            if not labels_ordered:
                logger.warning(
                    "Stage 2 — ranker %s returned unparseable ranking", ranker
                )
                return None
            # Map labels back to models; assign 0-based rank positions
            rank_dict: dict[str, int] = {}
            for pos, label in enumerate(labels_ordered):
                model = label_map.get(label)
                if model:
                    rank_dict[model] = pos
            return rank_dict
        except Exception as exc:
            logger.warning("Stage 2 — ranker %s failed: %s", ranker, exc)
            return None

    tasks = [_rank_one(ranker) for ranker in answers]
    results = await asyncio.gather(*tasks)
    return [r for r in results if r is not None]


# ---------------------------------------------------------------------------
# Stage 3 — Chairman synthesis
# ---------------------------------------------------------------------------

_SYNTHESIS_SYSTEM = (
    "You are the chairman of an AI council. "
    "You have collected independent answers from multiple AI models and peer rankings "
    "of those answers. Your task is to synthesize a single, authoritative, accurate, "
    "and well-reasoned final answer to the user's question. "
    "Draw on the best insights from each response, correct any errors you notice, "
    "and present a coherent answer. Do not mention individual model names."
)


def _build_synthesis_prompt(
    question: str,
    answers: dict[str, str],
    avg_ranks: dict[str, float],
) -> str:
    """Build the chairman's synthesis prompt, ordered by consensus rank (best first)."""
    sorted_models = sorted(
        answers.keys(),
        key=lambda m: avg_ranks.get(m, float("inf")),
    )
    labels = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    lines = [f"Original question: {question}\n\nCouncil responses (best-ranked first):\n"]
    for idx, model in enumerate(sorted_models):
        label = labels[idx]
        avg = avg_ranks.get(model, None)
        rank_str = f" (avg rank: {avg:.2f})" if avg is not None else ""
        lines.append(f"--- Response {label}{rank_str} ---\n{answers[model]}\n")
    lines.append(
        "\nUsing the above responses and their consensus rankings as guidance, "
        "provide the best possible final answer."
    )
    return "\n".join(lines)


async def _stage3_synthesize(
    client: httpx.AsyncClient,
    question: str,
    answers: dict[str, str],
    avg_ranks: dict[str, float],
    chairman: str,
    max_tokens: int,
    caller_system: str = "",
) -> str:
    """
    Chairman synthesizes a final answer.

    The caller's original ``system`` prompt (which may carry output-format
    constraints such as "SCORE: N" or "reply with ONLY the value") is appended
    to the chairman's instructions so the synthesized answer still honours the
    caller's required format — otherwise those constraints, applied only in
    Stage 1, would be silently lost at synthesis time.
    """
    prompt = _build_synthesis_prompt(question, answers, avg_ranks)
    system = _SYNTHESIS_SYSTEM
    if caller_system:
        system = (
            f"{_SYNTHESIS_SYSTEM}\n\n"
            f"The final answer MUST also obey these original instructions:\n{caller_system}"
        )
    return await _call_model(
        client,
        chairman,
        [{"role": "user", "content": prompt}],
        system=system,
        max_tokens=max_tokens,
    )


# ---------------------------------------------------------------------------
# Core async orchestrator
# ---------------------------------------------------------------------------


async def _run_council(
    question: str,
    system: str = "",
    models: Optional[list[str]] = None,
    chairman: str = CHAIRMAN,
    max_tokens: int = 512,
) -> str:
    """
    Full 3-stage council pipeline (async).

    :param question: The question to put before the council.
    :param system:   Optional system prompt for all models.
    :param models:   Council member model IDs (defaults to COUNCIL_MEMBERS).
    :param chairman: Chairman model ID (defaults to CHAIRMAN).
    :param max_tokens: Token budget for each model's answer and for the chairman.
    :returns: Chairman's synthesized answer string.
    """
    if models is None:
        models = COUNCIL_MEMBERS

    # When no OpenRouter key is present, every model is routed to the Anthropic
    # direct API — which only serves Anthropic-family models. Non-Anthropic
    # members (gpt-4o, gemini, llama) would 400 one by one, silently collapsing
    # the council to whatever Claude models remain. Filter them up front and
    # warn, so the degradation is explicit rather than a stream of failures.
    if not _openrouter_key():
        anthropic_models = [m for m in models if "anthropic" in m or "claude" in m]
        if anthropic_models != models:
            logger.warning(
                "OPENROUTER_API_KEY not set — council limited to Anthropic models "
                "%s (dropped %s).",
                anthropic_models,
                [m for m in models if m not in anthropic_models],
            )
        models = anthropic_models or models
        if not _anthropic_key():
            raise EnvironmentError(
                "Neither OPENROUTER_API_KEY nor ANTHROPIC_API_KEY is set — "
                "cannot run the council."
            )

    async with httpx.AsyncClient() as client:
        # --- Stage 1 ---
        logger.info("Council Stage 1: collecting independent answers from %d models", len(models))
        answers = await _stage1_answers(client, question, system, models, max_tokens)

        if not answers:
            raise RuntimeError("All council members failed to respond.")

        if len(answers) < 2:
            # Cannot meaningfully rank a single response; return it directly.
            sole_answer = next(iter(answers.values()))
            logger.warning(
                "Only 1 council member responded; skipping stages 2 & 3."
            )
            return sole_answer

        # --- Stage 2 ---
        logger.info("Council Stage 2: peer ranking by %d responding models", len(answers))
        rankings = await _stage2_rankings(client, question, answers)

        if rankings:
            avg_ranks = _aggregate_rankings(rankings)
        else:
            logger.warning("Stage 2 produced no valid rankings; using uniform ranking.")
            avg_ranks = {m: 0.0 for m in answers}

        # --- Stage 3 ---
        logger.info("Council Stage 3: chairman (%s) synthesizing final answer", chairman)
        final_answer = await _stage3_synthesize(
            client, question, answers, avg_ranks, chairman, max_tokens,
            caller_system=system,
        )
        return final_answer


# ---------------------------------------------------------------------------
# Public synchronous API
# ---------------------------------------------------------------------------


def ask_council(
    question: str,
    system: str = "",
    models: Optional[list[str]] = None,
    chairman: str = CHAIRMAN,
    max_tokens: int = 512,
) -> str:
    """
    Run the full 3-stage llm-council deliberation.

    Stage 1: All council members answer independently in parallel.
    Stage 2: Each member anonymously ranks the others' responses.
    Stage 3: Chairman synthesizes using all responses + rankings.

    :param question:   The question to answer.
    :param system:     Optional system prompt applied to all models.
    :param models:     Council member model IDs. Defaults to COUNCIL_MEMBERS.
    :param chairman:   Chairman model ID. Defaults to CHAIRMAN.
    :param max_tokens: Maximum tokens per model response (and for the synthesis).
    :returns: Chairman's final synthesized answer as a string.
    """
    return asyncio.run(
        _run_council(
            question=question,
            system=system,
            models=models,
            chairman=chairman,
            max_tokens=max_tokens,
        )
    )


def ask_council_brief(
    question: str,
    system: str = "",
    max_tokens: int = 128,
) -> str:
    """
    Same as ask_council but with a smaller token budget for short answers.

    Suited for Yes/No questions, numeric results, or any answer that should
    be concise (a few words or a sentence).

    :param question:   The question to answer.
    :param system:     Optional system prompt applied to all models.
    :param max_tokens: Maximum tokens per model response. Defaults to 128.
    :returns: Chairman's final synthesized answer as a string.
    """
    return ask_council(
        question=question,
        system=system,
        models=None,
        chairman=CHAIRMAN,
        max_tokens=max_tokens,
    )
