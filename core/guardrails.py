"""
Deterministic Guardrail Validator.

Per the Problem Statement architecture diagram:

    Energy Data + Operator Notes -> LLM Interpreter -> Guardrail Validator
        -> Math Optimizer -> Final Validator -> API Response

LLM output is UNTRUSTED structured data (section 08). Nothing produced by
llm.py is allowed to reach the optimizer without passing through here.
This module never raises on bad LLM output — it always degrades safely to
a valid `no_op` entry instead, per the "SAFE FAILURE" requirement.
"""

ALLOWED_DIRECTIVE_TYPES = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}


def _safe_no_op(note_index: int, explanation: str) -> dict:
    return {
        "note_index": note_index,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": explanation,
    }


def _clean_hours(raw_hours) -> list[int]:
    """Unique integers 0-23, ascending. Anything else is dropped."""
    if not isinstance(raw_hours, list):
        return []
    cleaned = set()
    for h in raw_hours:
        try:
            h_int = int(h)
        except (TypeError, ValueError):
            continue
        if 0 <= h_int <= 23:
            cleaned.add(h_int)
    return sorted(cleaned)


def validate_directive(raw: dict, note_index: int, battery_capacity_kwh: float) -> dict:
    """
    Validate a single raw LLM directive against the guardrails in section 08.
    Always returns a well-formed directive_interpretation entry.
    """
    if not isinstance(raw, dict):
        return _safe_no_op(note_index, "Malformed interpretation output; defaulted to no_op.")

    directive_type = raw.get("directive_type")
    explanation = str(raw.get("explanation") or "")[:500] or "No explanation provided."

    if directive_type not in ALLOWED_DIRECTIVE_TYPES:
        return _safe_no_op(
            note_index, f"Unsupported directive_type '{directive_type}'; defaulted to no_op."
        )

    if directive_type == "no_op":
        return _safe_no_op(note_index, explanation)

    adj = raw.get("structured_adjustment")
    if not isinstance(adj, dict):
        return _safe_no_op(note_index, "Missing structured_adjustment; defaulted to no_op.")

    hours = _clean_hours(adj.get("hours"))
    if not hours:
        return _safe_no_op(note_index, "No valid hours in structured_adjustment; defaulted to no_op.")

    if directive_type == "solar_reduction":
        try:
            factor = float(adj.get("factor"))
        except (TypeError, ValueError):
            return _safe_no_op(note_index, "Invalid solar_reduction factor; defaulted to no_op.")
        factor = max(0.0, min(1.0, factor))
        structured_adjustment = {"hours": hours, "factor": round(factor, 4)}

    elif directive_type == "minimum_battery_reserve":
        try:
            reserve = float(adj.get("minimum_energy_kwh"))
        except (TypeError, ValueError):
            return _safe_no_op(note_index, "Invalid minimum_energy_kwh; defaulted to no_op.")
        if reserve < 0 or reserve != reserve:  # NaN check
            return _safe_no_op(note_index, "Invalid minimum_energy_kwh; defaulted to no_op.")
        reserve = min(reserve, battery_capacity_kwh)
        structured_adjustment = {"hours": hours, "minimum_energy_kwh": round(reserve, 4)}

    elif directive_type == "max_grid_window":
        try:
            max_grid_kwh = float(adj.get("max_grid_kwh"))
        except (TypeError, ValueError):
            return _safe_no_op(note_index, "Invalid max_grid_kwh; defaulted to no_op.")
        if max_grid_kwh < 0 or max_grid_kwh != max_grid_kwh:
            return _safe_no_op(note_index, "Invalid max_grid_kwh; defaulted to no_op.")
        structured_adjustment = {"hours": hours, "max_grid_kwh": round(max_grid_kwh, 4)}

    elif directive_type in ("no_charge_window", "no_discharge_window"):
        structured_adjustment = {"hours": hours}

    else:  # pragma: no cover - unreachable, allowed set already checked
        return _safe_no_op(note_index, "Unhandled directive_type; defaulted to no_op.")

    return {
        "note_index": note_index,
        "applies": True,
        "directive_type": directive_type,
        "structured_adjustment": structured_adjustment,
        "explanation": explanation,
    }


def build_directive_interpretation(
    raw_results: list[dict], operator_notes: list[str], battery_capacity_kwh: float
) -> list[dict]:
    """
    Guarantees exactly one entry per operator note, in note_index order,
    even if the LLM stage returned too few, too many, duplicate, or
    malformed entries. This satisfies the "Interpretation coverage" metric
    in section 08 regardless of upstream failures.
    """
    by_index: dict[int, dict] = {}
    for i, raw in enumerate(raw_results):
        idx = raw.get("note_index", i) if isinstance(raw, dict) else i
        if not isinstance(idx, int) or not (0 <= idx < len(operator_notes)):
            idx = i
        if idx not in by_index:  # first entry wins; drop duplicates
            by_index[idx] = raw

    entries = []
    for note_index in range(len(operator_notes)):
        raw = by_index.get(note_index)
        if raw is None:
            entries.append(
                _safe_no_op(note_index, "No interpretation returned; defaulted to no_op.")
            )
        else:
            entries.append(validate_directive(raw, note_index, battery_capacity_kwh))
    return entries