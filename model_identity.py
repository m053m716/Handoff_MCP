"""Shared parsing of agent model descriptors into family/version/variant/effort.

This is the Python counterpart to the classifier in
`mcp/gui/dashboard/analytics/shared.js`. Both must agree: the dashboard buckets
rows by the labels produced here, so a divergence re-splits a single model across
several panels. When changing promotion or fallback rules, update both files and
their tests together.

Canonical labels are built by `mcp.lifecycle.canonical_model_label()` as
``"<Family> <version>[ <Variant>] <Effort>"`` -- except the ``gpt`` family, which
omits the family token entirely (``"5.6 Luna Extra High"``).
"""

from __future__ import annotations

import re
from typing import NamedTuple

# Display names, mirroring MODEL_REGISTRATION_FAMILIES in mcp/lifecycle.py.
MODEL_FAMILIES = ("GPT", "Opus", "Sonnet", "Haiku", "Fable", "Gemini", "Llama", "Mistral", "Grok")
# Longest-first so "Extra High" wins over the "High" suffix.
MODEL_EFFORTS = ("Extra High", "Medium", "Ultra", "High", "Max", "None", "Low")
MODEL_VERSION_RE = re.compile(r"^\d+(?:\.\d+){0,3}(?:[-_][A-Za-z0-9]+)?$")

# The gpt family drops its name in canonical labels, so a bare version leads the
# text. Re-attach the family name ("5.6 Luna" -> "GPT 5.6 Luna") so every family
# renders the same way: "<Family> <version>[ <Variant>]".
GPT_IMPLIED_FAMILY = "GPT"

# Legacy free-text descriptors often name only a major version ("Opus 4", "gpt-5").
# Promote those to the release actually in use so they merge with the canonical
# bucket instead of forming a near-duplicate lane.
MODEL_VERSION_PROMOTIONS = {"Opus": {"4": "4.8"}, "GPT": {"5": "5.5"}}
# Vendor-level catch-all when a descriptor names no version at all, as
# (family, version) pairs.
MODEL_VENDOR_FALLBACKS = {"anthropic": ("Opus", "4.8"), "openai": ("GPT", "5.5")}
# Same defaults keyed by family, for text that names a family but no version
# ("opus-session"). Derived so the two cannot drift apart.
MODEL_FAMILY_FALLBACK_VERSIONS = {fam: ver for fam, ver in MODEL_VENDOR_FALLBACKS.values()}
# Descriptors that predate effort registration carry no tier; assume the tier
# these agents actually ran at rather than leaving the field blank.
MODEL_DEFAULT_EFFORT = "High"
MODEL_UNKNOWN_LABEL = "Unknown"

# Legacy fallback for unregistered/free-text descriptors only.
MODEL_FAMILY_PATTERNS = (
    ("Opus", re.compile(r"opus[\s._-]*(\d+(?:\.\d+)?)?")),
    ("Sonnet", re.compile(r"sonnet[\s._-]*(\d+(?:\.\d+)?)?")),
    ("Haiku", re.compile(r"(?:claude[\s._-]*)?haiku[\s._-]*(\d+(?:\.\d+)?)?")),
    ("Fable", re.compile(r"fable[\s._-]*(\d+(?:\.\d+)?)?")),
    ("GPT", re.compile(r"gpt[\s._-]*(\d+(?:\.\d+)?)?")),
    ("Gemini", re.compile(r"gemini[\s._-]*(\d+(?:\.\d+)?)?")),
    ("Llama", re.compile(r"llama[\s._-]*(\d+(?:\.\d+)?)?")),
    ("Mistral", re.compile(r"mistral[\s._-]*(\d+(?:\.\d+)?)?")),
    ("Grok", re.compile(r"grok[\s._-]*(\d+(?:\.\d+)?)?")),
)
MODEL_FAMILY_DEFAULT_VERSIONS = {"Fable": "5", "GPT": "5.5", "Haiku": "4.5"}

_ANTHROPIC_RE = re.compile(r"claude|anthropic")
_OPENAI_RE = re.compile(r"openai|chatgpt|codex")


class ParsedModel(NamedTuple):
    family: str
    version: str
    variant: str
    effort: str


class ModelIdentity(NamedTuple):
    """Resolved identity for one descriptor.

    ``label`` excludes the effort tier so panels group by model; ``effort`` is the
    tier, defaulted to ``High`` when the descriptor carried none.
    """

    label: str
    family: str
    version: str
    variant: str
    effort: str

    @property
    def display(self) -> str:
        return f"{self.label} {self.effort}".strip()


def normalize_model_version(family: str, version: str) -> str:
    return MODEL_VERSION_PROMOTIONS.get(family, {}).get(version, version)


def _title_case_word(word: str) -> str:
    return word if word[:1].isdigit() else word[:1].upper() + word[1:].lower()


def _match_effort(tokens: list[str]) -> tuple[str, list[str]]:
    for effort in MODEL_EFFORTS:
        parts = effort.split(" ")
        if len(tokens) <= len(parts):
            continue
        if " ".join(tokens[-len(parts):]).lower() == effort.lower():
            return effort, tokens[: -len(parts)]
    return "", tokens


def parse_canonical_model_label(descriptor: str) -> ParsedModel | None:
    """Parse a canonical registration label, or return None if it does not match."""
    tokens = [t for t in re.sub(r"\s+", " ", str(descriptor or "").strip()).split(" ") if t]
    if not tokens:
        return None
    effort, rest = _match_effort(tokens)
    if not rest:
        return None
    family = ""
    version_index = 0
    head = next((f for f in MODEL_FAMILIES if f.lower() == rest[0].lower()), None)
    if head:
        family = head
        version_index = 1
    version = rest[version_index] if version_index < len(rest) else ""
    if not MODEL_VERSION_RE.match(version):
        return None
    if not family:
        family = GPT_IMPLIED_FAMILY
    variant = " ".join(_title_case_word(w) for w in rest[version_index + 1:])
    return ParsedModel(family, version, variant, effort)


def model_identity_label(parsed: ParsedModel) -> str:
    """Identity without the effort tier: "GPT 5.6 Luna", "Opus 4.8"."""
    version = normalize_model_version(parsed.family, parsed.version)
    stem = f"{parsed.family} {version}"
    return f"{stem} {parsed.variant}" if parsed.variant else stem


def classify_model_descriptor(descriptor: str) -> ModelIdentity | None:
    """Best-guess identity for any descriptor. None when nothing is recognizable."""
    text = str(descriptor or "").strip().lower()
    if not text:
        return None

    parsed = parse_canonical_model_label(descriptor)
    if parsed:
        return ModelIdentity(
            label=model_identity_label(parsed),
            family=parsed.family,
            version=normalize_model_version(parsed.family, parsed.version),
            variant=parsed.variant,
            effort=parsed.effort or MODEL_DEFAULT_EFFORT,
        )

    # Free text: pick the first family keyword that appears.
    for family, pattern in MODEL_FAMILY_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        version = normalize_model_version(
            family, match.group(1) or MODEL_FAMILY_DEFAULT_VERSIONS.get(family, "")
        )
        # Family named with no version and no default (e.g. "opus-session"): use
        # the vendor catch-all for that family rather than dropping the row to
        # Unknown, which would hide a known model from the panels.
        version = version or MODEL_FAMILY_FALLBACK_VERSIONS.get(family, "")
        if not version:
            continue
        return ModelIdentity(
            label=model_identity_label(ParsedModel(family, version, "", "")),
            family=family,
            version=version,
            variant="",
            effort=MODEL_DEFAULT_EFFORT,
        )

    # Bare vendor words carry no version; fall back to that vendor's default model.
    if _ANTHROPIC_RE.search(text):
        family, version = MODEL_VENDOR_FALLBACKS["anthropic"]
    elif _OPENAI_RE.search(text):
        family, version = MODEL_VENDOR_FALLBACKS["openai"]
    else:
        return None
    return ModelIdentity(
        label=model_identity_label(ParsedModel(family, version, "", "")),
        family=family,
        version=version,
        variant="",
        effort=MODEL_DEFAULT_EFFORT,
    )


def resolve_model_identity(*candidates: str) -> ModelIdentity | None:
    """Classify the first candidate descriptor that yields a recognizable model."""
    for candidate in candidates:
        identity = classify_model_descriptor(candidate)
        if identity:
            return identity
    return None


def canonical_effective_label(descriptor: str) -> str:
    """Effective Token-Usage grouping label for a single descriptor.

    Mirrors ``modelIdentity`` in ``mcp/gui/dashboard/analytics/shared.js``: a
    canonical registration label is normalized to its identity (effort stripped),
    otherwise the free-text classifier is used. Unrecognized text collapses to
    ``Unknown``. This is the exact identity Token Usage buckets rows by, so it is
    the identity the administrative rename/purge tools match against.
    """
    canonical = str(descriptor or "").strip()
    if canonical:
        parsed = parse_canonical_model_label(canonical)
        if parsed:
            return model_identity_label(parsed)
    identity = classify_model_descriptor(descriptor)
    return identity.label if identity else MODEL_UNKNOWN_LABEL


def row_effective_label(
    *,
    canonical_model_label: str = "",
    model_descriptor: str = "",
    agent_label: str = "",
    agent_id: str = "",
) -> str:
    """Effective label for a token-usage / task-state row.

    Preference order matches the dashboard's ``modelIdentity`` +
    ``firstDescriptor``: the stored canonical label first, then the free-text
    descriptor, then the legacy ``agent_label``, then the ``agent_id``.
    """
    canonical = str(canonical_model_label or "").strip()
    if canonical:
        parsed = parse_canonical_model_label(canonical)
        if parsed:
            return model_identity_label(parsed)
        return canonical
    for candidate in (model_descriptor, agent_label, agent_id):
        text = str(candidate or "").strip()
        if text:
            return canonical_effective_label(text)
    return MODEL_UNKNOWN_LABEL
