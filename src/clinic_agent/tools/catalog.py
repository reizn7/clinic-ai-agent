"""Clinic-configured data: entity definitions + custom catalog records.

Implements the agent (reader) side of the console's clinic data contract
(receptionist-agent-platform/docs/clinic-data-contract.md):

* ``entity_definitions`` — per clinic, the pages and fields staff configured.
  System pages (``clinic``, ``doctor``, ``patient``) add fields under each
  document's ``attributes``; custom pages (lab tests, services, …) live in
  ``records``.
* The agent only ever sees a field when it is ``agentVisible`` and not ``pii``
  and not ``archived``. Patient attributes are never read here.
* Reads are lenient: a value that doesn't match its field type is dropped (and
  logged), never allowed to fail a patient's turn.

Definitions are cached for ``CACHE_TTL_SECONDS`` so console edits reach patients
within a minute without a redeploy. All functions are synchronous (pymongo);
callers use ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

from clinic_agent.config import settings
from clinic_agent.db import create_index_safe, get_db

log = logging.getLogger("clinic_agent.tools.catalog")

DEFINITIONS = "entity_definitions"
RECORDS = "records"
SYSTEM_TYPES = frozenset({"clinic", "doctor", "patient"})
CACHE_TTL_SECONDS = 60.0
SEARCH_LIMIT = 10
_SCAN_LIMIT = 500  # catalogs are clinic-sized; filter in Python after this many

_TEXT_TYPES = frozenset({"text", "longtext", "email", "phone", "url", "select"})
_LIST_TYPES = frozenset({"multiselect", "tags"})
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, dict]]] = {}  # clinicId → (loaded_at, defs)


def ensure_indexes() -> None:
    db = get_db()
    create_index_safe(
        db[DEFINITIONS],
        [("clinicId", 1), ("entityType", 1)],
        unique=True,
        name="uniq_clinic_entity_type",
    )
    create_index_safe(
        db[RECORDS],
        [("clinicId", 1), ("entityType", 1), ("isActive", 1)],
        name="clinic_type_active",
    )


# ---- definitions ------------------------------------------------------------


def _load_definitions(clinic_id: str) -> dict[str, dict]:
    defs: dict[str, dict] = {}
    for doc in get_db()[DEFINITIONS].find({"clinicId": clinic_id}):
        etype = doc.get("entityType")
        if not isinstance(etype, str) or doc.get("archived"):
            continue
        defs[etype] = doc
    return defs


def get_definitions(clinic_id: str | None = None) -> dict[str, dict]:
    """entityType → definition for the clinic (non-archived), cached briefly."""
    clinic_id = clinic_id or settings.CLINIC_ID
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(clinic_id)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
    defs = _load_definitions(clinic_id)
    with _cache_lock:
        _cache[clinic_id] = (now, defs)
    return defs


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def visible_fields(definition: dict | None) -> list[dict]:
    """Fields the agent may read: agentVisible, not pii, not archived; in order."""
    fields = (definition or {}).get("fields") or []
    out = [
        f
        for f in fields
        if isinstance(f, dict)
        and isinstance(f.get("key"), str)
        and f.get("agentVisible")
        and not f.get("pii")
        and not f.get("archived")
    ]
    return sorted(out, key=lambda f: f.get("order", 0))


def _coerce(field: dict, value: Any) -> Any:
    """The value if it matches the field type, else None (lenient read)."""
    if value is None:
        return None
    ftype = field.get("type")
    if ftype in _TEXT_TYPES:
        return value if isinstance(value, str) and value.strip() else None
    if ftype == "number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        return value if ok else None
    if ftype == "boolean":
        return value if isinstance(value, bool) else None
    if ftype == "date":
        return value if isinstance(value, str) and _DATE_RE.match(value) else None
    if ftype in _LIST_TYPES:
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return value or None
        return None
    return None  # unknown type: never guess


def render_attributes(definition: dict | None, attributes: Any, *, where: str = "") -> dict:
    """Agent-visible attributes as {field label: value}, mismatches dropped."""
    if not isinstance(attributes, dict):
        return {}
    out: dict[str, Any] = {}
    for field in visible_fields(definition):
        raw = attributes.get(field["key"])
        value = _coerce(field, raw)
        if value is None:
            if raw is not None:
                log.warning(
                    "skipping %s.%s: %r doesn't match type %s",
                    where or "record",
                    field["key"],
                    raw,
                    field.get("type"),
                )
            continue
        out[field.get("label") or field["key"]] = value
    return out


# ---- custom catalogs (records) -------------------------------------------------


def agent_catalogs(clinic_id: str | None = None) -> list[dict]:
    """Custom pages the agent may search: [{catalog, label, description}]."""
    out = []
    for etype, d in sorted(get_definitions(clinic_id).items()):
        if etype in SYSTEM_TYPES or d.get("kind") == "system" or not d.get("agentEnabled"):
            continue
        out.append(
            {
                "catalog": etype,
                "label": d.get("label") or etype,
                "description": d.get("description") or "",
            }
        )
    return out


def _resolve_catalog(name: str, defs: dict[str, dict]) -> str | None:
    """Accept the slug or the (singular/plural) label, case-insensitively."""
    wanted = (name or "").strip().lower()
    for etype, d in defs.items():
        names = {etype, str(d.get("label", "")).lower(), str(d.get("singularLabel", "")).lower()}
        if wanted in names or wanted.replace(" ", "_") == etype:
            return etype
    return None


_PUNCT_RE = re.compile(r"[^\w\s]+")


def _norm(text: str) -> str:
    """Lowercase with punctuation turned into spaces ("Check-up" → "check up")."""
    return " ".join(_PUNCT_RE.sub(" ", text.lower()).split())


def _searchable_text(item: dict) -> str:
    """Normalized text plus a no-spaces copy, so "checkup" finds "check-up"."""
    parts: list[str] = []
    for v in item.values():
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, list):
            parts.extend(x for x in v if isinstance(x, str))
    spaced = _norm(" ".join(parts))
    return f"{spaced} {spaced.replace(' ', '')}"


def _query_terms(query: str) -> list[str]:
    """Normalized, de-spaced words; punctuation inside a word is dropped."""
    return [w for w in (_PUNCT_RE.sub("", t) for t in (query or "").lower().split()) if w]


def _term_hits(term: str, text: str) -> bool:
    """Substring match, also trying the singular of a plural ("checkups")."""
    if term in text:
        return True
    return len(term) > 3 and term.endswith("s") and term[:-1] in text


def search_catalog(catalog: str, query: str = "", clinic_id: str | None = None) -> dict:
    """Search one agent-enabled custom page; return up to ``SEARCH_LIMIT`` rows.

    Every query word must match (ignoring case, punctuation and hyphens). If no
    row matches them all, the rows matching the most words come back instead,
    flagged ``partial_match`` — speech-to-text and patients rarely use the
    clinic's exact wording.
    """
    clinic_id = clinic_id or settings.CLINIC_ID
    enabled = {c["catalog"] for c in agent_catalogs(clinic_id)}
    defs = {k: v for k, v in get_definitions(clinic_id).items() if k in enabled}
    etype = _resolve_catalog(catalog, defs)
    if etype is None:
        return {
            "error": f"No catalog named '{catalog}'.",
            "available_catalogs": sorted(defs),
        }
    definition = defs[etype]
    title_key = definition.get("titleField")

    items: list[dict] = []
    cursor = (
        get_db()[RECORDS]
        .find({"clinicId": clinic_id, "entityType": etype, "isActive": True})
        .limit(_SCAN_LIMIT)
    )
    for doc in cursor:
        attrs = doc.get("attributes") or {}
        item = render_attributes(definition, attrs, where=f"{etype}:{doc.get('_id')}")
        title = attrs.get(title_key) if title_key else None
        if isinstance(title, str) and title.strip():
            item = {"name": title, **{k: v for k, v in item.items() if v != title}}
        if item:
            items.append(item)

    def by_name(i: dict) -> str:
        return str(i.get("name", "")).lower()

    terms = _query_terms(query)
    scored = []
    for item in items:
        text = _searchable_text(item)
        scored.append((sum(_term_hits(t, text) for t in terms), item))

    matches = sorted((i for n, i in scored if n == len(terms)), key=by_name)
    result = {
        "catalog": etype,
        "label": definition.get("label") or etype,
        "query": query,
    }
    if not matches and len(terms) > 1:
        best = max(n for n, _ in scored) if scored else 0
        if best:
            partial = sorted((i for n, i in scored if n == best), key=by_name)
            return {
                **result,
                "partial_match": True,
                "note": "Nothing matched every word; these are the closest items. "
                "Check they are what the patient means before answering.",
                "total_matches": len(partial),
                "results": partial[:SEARCH_LIMIT],
            }
    return {**result, "total_matches": len(matches), "results": matches[:SEARCH_LIMIT]}


# ---- clinic details ---------------------------------------------------------------


def clinic_detail_fields(clinic_id: str | None = None) -> list[dict]:
    """Agent-visible fields staff added to the clinic page, in order."""
    return visible_fields(get_definitions(clinic_id).get("clinic"))


def pick_details(details: dict, wanted: str) -> dict:
    """The subset of ``{label: value}`` that ``wanted`` refers to.

    Matches the label as a phrase either way round ("insurance" →
    "Insurance / TPAs accepted (cashless)"), then falls back to any shared word
    of 4+ letters, so a loose topic name still finds the right detail.
    """
    q = _norm(wanted)
    if not q:
        return {}
    hits = {k: v for k, v in details.items() if q in _norm(k) or _norm(k) in q}
    if hits:
        return hits
    words = {w for w in q.split() if len(w) >= 4}
    return {k: v for k, v in details.items() if words & set(_norm(k).split())}


# ---- prompt section -------------------------------------------------------------


def prompt_section(clinic_id: str | None = None) -> str:
    """Clinic-specific prompt text: catalogs + clinic detail topics ("" if none)."""
    sections = []
    catalogs = agent_catalogs(clinic_id)
    if catalogs:
        lines = [
            "# Clinic catalogs",
            "Besides doctors and appointments, this clinic has these catalogs. When a patient "
            "asks about one (availability, price, preparation, …), call search_catalog with the "
            "catalog name and a short query, and answer ONLY from what it returns. If nothing "
            "matches, try a shorter or differently worded query (and the clinic details below) "
            "before saying the clinic doesn't list it; then offer to connect them to staff.",
        ]
        for c in catalogs:
            desc = f" — {c['description']}" if c["description"] else ""
            lines.append(f"- {c['catalog']} ({c['label']}){desc}")
        sections.append("\n".join(lines))

    labels = [f.get("label") or f["key"] for f in clinic_detail_fields(clinic_id)]
    if labels:
        lines = [
            "# Clinic details",
            "Beyond name, timings, address and phone, the clinic has these details. To read "
            "one, call get_clinic_info with detail set to its name (e.g. which insurers are "
            "accepted, payment modes, parking). Check them before saying the clinic doesn't "
            "offer or accept something.",
        ]
        lines.extend(f"- {label}" for label in labels)
        sections.append("\n".join(lines))
    return "\n\n".join(sections)
