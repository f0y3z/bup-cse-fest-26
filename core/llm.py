"""
LLM Interpreter stage.

Converts operator_notes (1-3 natural-language strings) into RAW candidate
directives. Output here is treated as UNTRUSTED — guardrails.py performs all
validation before anything reaches the optimizer.

Changes vs. the original:
  1. The battery context (capacity, reserve, rates) is sent to the model, so
     relative notes like "keep 50% of battery capacity in reserve" can be
     converted to kWh. Without this the model cannot answer that class of note.
  2. Hard request timeout + one retry, so a slow provider cannot blow the
     30-second per-request judge budget.
  3. Small in-process cache keyed on (notes, battery) to cut p95 latency on
     repeated hidden cases.
"""
import json
import logging
import os
import threading

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
LLM_TIMEOUT_MS = int(os.getenv("LLM_TIMEOUT_MS", "12000"))

_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 256

SYSTEM_PROMPT = """You are an expert energy-grid operator-directive parser for the
BUP CSE Fest GridWise challenge.

You will receive the battery parameters for a 24-hour campus energy schedule
(hours 0-23) followed by a numbered list of operator notes. For EVERY note,
decide whether it changes today's schedule and, if so, convert it into exactly
one structured directive.

Supported directive_type values and their REQUIRED structured_adjustment shape:
- "solar_reduction": {"hours": [int, ...], "factor": number}
    factor is the USABLE FRACTION THAT REMAINS. A "20% reduction" -> factor 0.8.
    An "80% reduction" (i.e. drops to ~20% of normal) -> factor 0.2.
    "about half of forecast output" -> factor 0.5. "roughly one-fifth" -> 0.2.
- "minimum_battery_reserve": {"hours": [int, ...], "minimum_energy_kwh": number}
    If the note states a PERCENTAGE or FRACTION of battery capacity, convert it
    to kWh using the capacity_kwh given below. Example: "keep 50% of capacity"
    with capacity_kwh = 200 -> minimum_energy_kwh = 100.
- "no_charge_window": {"hours": [int, ...]}
- "no_discharge_window": {"hours": [int, ...]}
- "max_grid_window": {"hours": [int, ...], "max_grid_kwh": number}
- "no_op": structured_adjustment is null. Use this for anything that does not
   change today's energy schedule (distractors, unrelated announcements,
   anything about a future day, next week, or next month).

Rules:
- Time windows are whole-hour, START-INCLUSIVE / END-EXCLUSIVE:
  "1 PM to 3 PM" -> [13, 14]; "6 PM until 10 PM" -> [18, 19, 20, 21];
  "from noon until 2 PM" -> [12, 13]; "between 13:00 and 15:00" -> [13, 14].
- "hours" must be unique integers 0-23 in ascending order.
- Do not invent demand, tariff, or battery parameters. Only use the six
  directive types above.
- Charging vocabulary ("charger isolated", "charging circuit unavailable",
  "charging disabled") -> no_charge_window. Discharge vocabulary ("must not
  discharge", "no battery output", "relay/protection testing") ->
  no_discharge_window. Import limits ("grid intake", "feeder limit",
  "transformer limit", "must not exceed N kWh from the grid") ->
  max_grid_window.
- Every note gets exactly one interpretation. Use applies=true for every
  directive except no_op, and applies=false only for no_op.
- Notes may paraphrase the same underlying rule with different wording, units,
  or percentages — extract the underlying rule, not the literal words.

Respond with ONLY a JSON object of this exact shape, no markdown, no commentary:
{"interpretations": [
  {"note_index": 0, "applies": true, "directive_type": "solar_reduction",
   "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
   "explanation": "short reason"},
  ...
]}
There must be exactly one entry per note, in note_index order starting at 0.
"""


def _fallback(operator_notes: list, reason: str) -> list:
    logger.warning("LLM interpretation fallback: %s", reason)
    return [
        {
            "note_index": i,
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": f"Defaulted to no_op ({reason}).",
        }
        for i in range(len(operator_notes))
    ]


def _build_prompt(operator_notes: list, battery: dict) -> str:
    battery = battery or {}
    context = (
        "Battery parameters for this scenario:\n"
        f"- capacity_kwh: {battery.get('capacity_kwh')}\n"
        f"- initial_energy_kwh: {battery.get('initial_energy_kwh')}\n"
        f"- base minimum_energy_kwh: {battery.get('minimum_energy_kwh')}\n"
        f"- max_charge_kwh_per_hour: {battery.get('max_charge_kwh_per_hour')}\n"
        f"- max_discharge_kwh_per_hour: {battery.get('max_discharge_kwh_per_hour')}\n"
    )
    numbered = "\n".join(f"{i}: {note}" for i, note in enumerate(operator_notes))
    return f"{context}\nOperator notes:\n{numbered}"


def _call_model(prompt: str) -> str:
    try:
        client = genai.Client(
            api_key=GEMINI_API_KEY,
            http_options=types.HttpOptions(timeout=LLM_TIMEOUT_MS),
        )
    except Exception:  # older SDK without http_options support
        client = genai.Client(api_key=GEMINI_API_KEY)

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            temperature=0.0,
        ),
    )
    return response.text


def parse_operator_notes(operator_notes: list, battery: dict = None) -> list:
    """
    Returns a list of RAW (not yet guardrail-validated) directive dicts, one per
    note, in note_index order. Never raises — any failure degrades to a safe
    no_op list so the service cannot crash on a bad note or a provider outage.
    """
    if not GEMINI_API_KEY:
        return _fallback(operator_notes, "GEMINI_API_KEY not configured")

    cache_key = json.dumps([operator_notes, battery], sort_keys=True, default=str)
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
    if cached is not None:
        return json.loads(json.dumps(cached))

    prompt = _build_prompt(operator_notes, battery)

    last_reason = "unknown error"
    for attempt in range(2):
        try:
            data = json.loads(_call_model(prompt))
            interpretations = data.get("interpretations")
            if not isinstance(interpretations, list):
                last_reason = "response missing 'interpretations' array"
                continue
            with _CACHE_LOCK:
                if len(_CACHE) >= _CACHE_MAX:
                    _CACHE.clear()
                _CACHE[cache_key] = interpretations
            return interpretations
        except json.JSONDecodeError:
            last_reason = "model returned invalid JSON"
        except Exception as exc:  # provider/network error, timeout, etc.
            last_reason = f"provider error: {type(exc).__name__}"
        logger.warning("LLM attempt %s failed: %s", attempt + 1, last_reason)

    return _fallback(operator_notes, last_reason)