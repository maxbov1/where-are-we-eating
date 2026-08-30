"""Deterministic confidence scoring for aggregated group preferences.

This runs before any agent call. It converts the vote tallies and consensus
labels that ``aggregate_survey`` already computes into a single confidence
picture: a per-dimension score, an overall band, and plain-language notes the
agent (or a future UI) can show without re-deriving anything.

Nothing here talks to a model or a provider. Same input, same output.
"""
from __future__ import annotations

# Consensus label -> base confidence for that dimension. The labels come
# straight from ``aggregate_survey``'s ``consensus()`` helper.
CONSENSUS_SCORE = {
    "unanimous": 1.0,
    "strong": 0.8,
    "moderate": 0.55,
    "split": 0.3,
    "tie": 0.2,
    "no responses": 0.0,
}

# Below this many responses every dimension score is discounted linearly:
# 1 of 4 responses -> x0.25, 4+ -> x1.0. A tiny group is never "high" confidence.
FULL_SIGNAL_RESPONSES = 4

# Consensus labels that mean "the group has not settled this".
_UNSETTLED = {"split", "tie"}

# Human-facing names for the known dimensions; unknown keys fall back to Title Case.
_LABELS = {
    "dates": "Dates",
    "times": "Times",
    "cuisine": "Cuisine",
    "price": "Budget",
    "vibe": "Vibe",
    "distance": "Distance",
    "dietary": "Dietary needs",
}


def _band(score: float) -> str:
    if score <= 0.0:
        return "none"
    if score >= 0.7:
        return "high"
    if score >= 0.45:
        return "medium"
    return "low"


def _label_for(dimension: str) -> str:
    return _LABELS.get(dimension, dimension.replace("_", " ").title())


def _describe_split(name: str, votes: dict[str, int]) -> str:
    ordered = sorted(votes.items(), key=lambda item: (-item[1], item[0]))[:3]
    parts = ", ".join(f"{value} ({count})" for value, count in ordered)
    return f"{name} is unsettled: {parts}."


def score_confidence(
    response_count: int,
    votes_by_dimension: dict[str, dict[str, int]],
    consensus_by_dimension: dict[str, str],
) -> dict:
    """Return a confidence block derived only from tallies already computed.

    ``votes_by_dimension`` is ``{dimension: {option: count}}`` (the aggregate's
    ``preference_summary``). ``consensus_by_dimension`` is ``{dimension: label}``
    using the same labels as ``aggregate_survey``. A dimension with no answers
    should still appear in ``consensus_by_dimension`` as ``"no responses"``.
    """
    sample_factor = (
        min(1.0, response_count / FULL_SIGNAL_RESPONSES) if response_count else 0.0
    )

    dimensions: dict[str, dict] = {}
    for dimension, label in consensus_by_dimension.items():
        base = CONSENSUS_SCORE.get(label, CONSENSUS_SCORE["split"])
        dimensions[dimension] = {
            "score": round(base * sample_factor, 2),
            "consensus": label,
        }

    if dimensions:
        overall_score = round(
            sum(item["score"] for item in dimensions.values()) / len(dimensions), 2
        )
        weakest = min(dimensions, key=lambda key: dimensions[key]["score"])
    else:
        overall_score = 0.0
        weakest = None

    notes: list[str] = []
    if response_count == 0:
        notes.append("No responses yet.")
    elif response_count < FULL_SIGNAL_RESPONSES:
        plural = "" if response_count == 1 else "s"
        notes.append(
            f"Only {response_count} response{plural}; treat rankings as tentative."
        )
    for dimension, item in dimensions.items():
        name = _label_for(dimension)
        if item["consensus"] in _UNSETTLED and votes_by_dimension.get(dimension):
            notes.append(_describe_split(name, votes_by_dimension[dimension]))
        elif item["consensus"] == "no responses":
            notes.append(f"No answers for {name.lower()}.")

    return {
        "overall": {"score": overall_score, "label": _band(overall_score)},
        "response_count": response_count,
        "dimensions": dimensions,
        "weakest_dimension": weakest,
        "notes": notes,
    }
