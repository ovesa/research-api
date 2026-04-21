import json
from datetime import datetime, timezone

import httpx
import structlog

from app.services.database import get_pool

logger = structlog.get_logger(__name__)

extraction_prompt_version = "v2"

# Added field-level hallucination guards for the riskiest fields.
system_prompt = """You are a precise scientific metadata extractor specialising in heliophysics.

You have technical domain knowledge of: helioseismology, MHD theory, solar dynamo, inertial modes,
Rossby waves, and instruments including SDO, GONG, and MDI. You are familiar with the work of Gizon, 
Löptien, Hanasoge, Dikpati, Proxauf, and Hathaway.

Your job is extraction, not interpretation. You report only what the abstract explicitly states.
You do not infer, generalise, or fill gaps from your domain knowledge. When information is absent, 
return [] for list fields and "" for string fields AND never a guess.

FIELD RULES - where hallucination risk is highest:

instruments: Only list instruments the abstract explicitly names. "Solar observations" without
a named instrument, return []. Do not infer SDO or MDI from context. They must be stated. HMI is an
instrument on SDO, so "SDO/HMI" or "Helioseismic and Magnetic Imager" counts as HMI. "SDO" alone 
does not. 

azimuthal_orders: If applicable, mention the m values used in the abstract. Do not infer azimuthal 
order from the wave type or method described. For example, if the abstract says "we study equatorial 
Rossby waves but does not mention m values, return [] for this field. If the abstract says "we analyze 
modes with m=1 and m=6  to m=10", return ["m=1", "m=6 to m=10"]. A range such as "3<=m<=10" is okay. 
Even the usage of high vs low m counts  as a mention, but return as ["high m"] or ["low m"].

numerical_values: Only extract numbers explicitly stated with units in the abstract. Do not convert, 
approximate, or derive values.

confirms_previous_work / contradicts_previous_work: Only populate if the abstract explicitly names a 
prior paper or author. Do not infer agreement from similar methodology.

EXTRACTION vs INTERPRETATION:
All fields except researcher_summary and extraction_notes are EXTRACTION fields — report only what is
explicitly stated, no inference allowed. researcher_summary and extraction_notes are INTERPRETATION fields 
— for these two only, you may draw on domain expertise. Clearly distinguish what the paper claims vs 
your assessment.

THIN ABSTRACTS:
If the abstract is very short or uninformative, return mostly empty fields and note this in extraction_notes. 
A sparse extraction of a thin abstract is correct. Never invent details to fill the schema."""

# Result arrives as a Python dict
# Claude is forced through this typed schema and cannot drift into prose.
extraction_tool_schema = {
    "name": "extract_paper_metadata",
    "description": "Extract structured scientific metadata from a heliophysics paper abstract.",
    "input_schema": {
        "type": "object",
        "required": [
            "central_contribution",
            "relevance_to_solar_inertial_modes",
            "data_type",
            "methods",
            "key_findings",
            "instruments",
            "wave_types",
            "open_questions",
            "researcher_summary",
            "extraction_notes",
            "confidence",
            "data_gaps",
        ],
        "properties": {
            "central_contribution": {
                "type": "string",
                "description": "One sentence summarising the paper's main contribution to the field.",
            },
            "relevance_to_solar_inertial_modes": {
                "type": "string",
                "enum": ["primary", "secondary", "peripheral"],
                "description": (
                    "primary = inertial modes, Rossby waves, high-latitude inertial modes are the main subject; "
                    "secondary = related solar dynamics that directly informs inertial mode research; "
                    "peripheral = mentions inertial modes briefly but is primarily about something else."
                ),
            },
            "data_type": {
                "type": "string",
                "enum": [
                    "observational",
                    "theoretical",
                    "computational",
                    "review",
                    "mixed",
                ],
            },
            "methods": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific methods or techniques used, e.g. ring diagram analysis, time distance analysis, mode coupling.",
            },
            "key_findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "finding": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": [
                                "detection",
                                "measurement",
                                "constraint",
                                "theoretical",
                                "null_result",
                            ],
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["definitive", "tentative", "marginal"],
                        },
                    },
                    "required": ["finding", "type", "confidence"],
                },
            },
            "instruments": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Instruments explicitly named in the abstract. Do not infer.",
            },
            "wave_types": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Wave or mode types studied, e.g. equatorial Rossby waves, high-latitude inertial modes.",
            },
            "solar_region": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Solar regions studied, e.g. convection zone, tachocline, photosphere.",
            },
            "azimuthal_orders": {
                "type": "array",
                "items": {"type": "string"},
                "description": "m values stated numerically in the abstract. Do not infer from wave type.",
            },
            "physical_parameters": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Physical quantities studied, e.g. differential rotation, meridional flow, eigenfrequency.",
            },
            "measured_quantities": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Quantities directly measured from data, e.g. mode frequency, phase velocity.",
            },
            "constrained_quantities": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Quantities inferred or constrained by results, e.g. superadiabaticity, turbulent viscosity.",
            },
            "theoretical_framework": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Physical models used, e.g. shallow water model, MHD, quasi-geostrophic theory.",
            },
            "detection_method": {
                "type": "string",
                "description": "How modes were detected, e.g. ring diagram analysis, power spectrum analysis. Empty string if not applicable.",
            },
            "observational_technique": {
                "type": "string",
                "enum": [
                    "helioseismology",
                    "spectroscopy",
                    "imaging",
                    "magnetogram",
                    "dopplergram",
                    "simulation_only",
                    "not_applicable",
                ],
            },
            "depth_range": {
                "type": "string",
                "description": "Depth range studied if mentioned, e.g. 0-30 Mm. Empty string if not mentioned.",
            },
            "radial_order": {
                "type": "string",
                "description": "Radial order n if mentioned, e.g. n=0, n=1. Empty string if not mentioned.",
            },
            "dispersion_relation_discussed": {
                "type": "string",
                "enum": ["yes", "no"],
            },
            "eigenfunction_computed": {
                "type": "string",
                "enum": ["yes", "no"],
            },
            "mode_identification_method": {
                "type": "string",
                "description": "How modes are identified, e.g. frequency matching, power spectrum peaks. Empty string if not applicable.",
            },
            "numerical_values": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "quantity": {"type": "string"},
                        "value": {"type": "string"},
                        "unit": {"type": "string"},
                    },
                    "required": ["quantity", "value", "unit"],
                },
                "description": "Only numbers explicitly stated with units in the abstract. Do not derive.",
            },
            "solar_cycle_phase": {
                "type": "string",
                "description": "Solar cycle context if mentioned, e.g. solar minimum, cycle 24, SC24. Empty string if not mentioned.",
            },
            "cycle_dependence": {
                "type": "string",
                "enum": ["yes", "no", "partial", "not_mentioned"],
            },
            "solar_activity_level": {
                "type": "string",
                "enum": [
                    "solar_minimum",
                    "solar_maximum",
                    "rising_phase",
                    "declining_phase",
                    "multiple_cycles",
                    "not_mentioned",
                ],
            },
            "magnetic_field_considered": {
                "type": "string",
                "enum": ["yes", "no"],
            },
            "time_period": {
                "type": "string",
                "description": "Observational time range if mentioned, e.g. 2010-2024 or 14 years. Empty string if not mentioned.",
            },
            "agrees_with_theory": {
                "type": "string",
                "enum": ["yes", "no", "partial", "not_applicable"],
            },
            "theoretical_prediction_tested": {
                "type": "string",
                "description": "Which theoretical prediction was tested, e.g. shallow water model. Empty string if not applicable.",
            },
            "confirms_previous_work": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Only populate if the abstract explicitly names a prior paper or author it confirms.",
            },
            "contradicts_previous_work": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Only populate if the abstract explicitly names a prior paper or author it disputes.",
            },
            "open_questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Unresolved questions the authors themselves flag, e.g. 'remains unclear', 'future observations needed'. Do not invent.",
            },
            "researcher_summary": {
                "type": "string",
                "description": "2-3 sentences on why this paper matters for solar inertial mode research, what gap it fills, and what a researcher should take away. This is an interpretation field — domain expertise welcome here.",
            },
            "extraction_notes": {
                "type": "string",
                "description": "Any ambiguity or limitation in this extraction, e.g. 'Abstract too brief to extract meaningful metadata.' Empty string if extraction is clear.",
            },
            "confidence": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "description": (
                    "How confident are you in the quality of this extraction overall. "
                    "low = abstract is very short, missing key fields, or ambiguous; "
                    "medium = abstract is adequate but some fields are uncertain; "
                    "high = abstract is detailed and all required fields are clearly populated."
                ),
            },
            "data_gaps": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Limitations or missing data the authors themselves identify, "
                    "e.g. 'only covers low m modes', 'limited to cycle 24', "
                    "'no magnetic field effects included'. "
                    "Different from open_questions — data_gaps are about what the "
                    "study could not measure or include, not future research directions. "
                    "Do not invent. Empty list if none mentioned."
                ),
            },
        },
    },
}


async def get_extraction(identifier: str) -> dict | None:
    """Return cached extraction for a paper, or None if not yet extracted.

    Args:
        identifier (str): ADS bibcode or arXiv ID of the paper.

    Returns:
        dict | None: Extraction result if cached, None otherwise.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM extractions WHERE identifier = $1",
            identifier,
        )
    if not row:
        return None
    return dict(row)


async def save_extraction(
    identifier: str,
    result: dict,
    raw_response: str,
    prompt_version: str = extraction_prompt_version,
) -> None:
    """Saves a Claude extraction result to the extractions table in Postgres.
    Uses INSERT ... ON CONFLICT DO UPDATE so re-running extraction on the
    same paper overwrites the old row rather than raising a duplicate key
    error. If you bump extraction_prompt_version and re-process a paper,
    the improved result replaces the stale one.

    Args:
        identifier: ADS bibcode or arXiv ID of the paper.

        result: Structured extraction dict returned by extract_abstract().
                    Keys match the columns of the extractions table exactly.

        raw_response: JSON-serialised extraction result, stored in the
                        raw_response column for debugging. If an extraction
                        looks wrong, inspect this column to see exactly what
                        Claude returned.

        prompt_version: Version string of the prompt that produced this result.
                            Defaults to extraction_prompt_version so call sites
                            rarely need to pass it explicitly. Stored in Postgres
                            so you can find stale rows after a prompt improvement:

                            SELECT identifier FROM extractions WHERE prompt_version = 'v1'.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO extractions (
                identifier,
                central_contribution,
                relevance_to_solar_inertial_modes,
                data_type,
                methods,
                key_findings,
                instruments,
                wave_types,
                solar_region,
                azimuthal_orders,
                physical_parameters,
                measured_quantities,
                constrained_quantities,
                theoretical_framework,
                detection_method,
                observational_technique,
                depth_range,
                radial_order,
                dispersion_relation_discussed,
                eigenfunction_computed,
                mode_identification_method,
                numerical_values,
                solar_cycle_phase,
                cycle_dependence,
                solar_activity_level,
                magnetic_field_considered,
                time_period,
                agrees_with_theory,
                theoretical_prediction_tested,
                confirms_previous_work,
                contradicts_previous_work,
                open_questions,
                researcher_summary,
                extraction_notes,
                raw_response,
                extracted_at,
                prompt_version,
                confidence,
                data_gaps
            )
            VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
    $11, $12, $13, $14, $15, $16, $17, $18, $19, $20,
    $21, $22, $23, $24, $25, $26, $27, $28, $29, $30,
    $31, $32, $33, $34, $35, $36, $37, $38, $39
)
            ON CONFLICT (identifier) DO UPDATE SET
                central_contribution = EXCLUDED.central_contribution,
                relevance_to_solar_inertial_modes = EXCLUDED.relevance_to_solar_inertial_modes,
                data_type = EXCLUDED.data_type,
                methods = EXCLUDED.methods,
                key_findings = EXCLUDED.key_findings,
                instruments = EXCLUDED.instruments,
                wave_types = EXCLUDED.wave_types,
                solar_region = EXCLUDED.solar_region,
                azimuthal_orders = EXCLUDED.azimuthal_orders,
                physical_parameters = EXCLUDED.physical_parameters,
                measured_quantities = EXCLUDED.measured_quantities,
                constrained_quantities = EXCLUDED.constrained_quantities,
                theoretical_framework = EXCLUDED.theoretical_framework,
                detection_method = EXCLUDED.detection_method,
                observational_technique = EXCLUDED.observational_technique,
                depth_range = EXCLUDED.depth_range,
                radial_order = EXCLUDED.radial_order,
                dispersion_relation_discussed = EXCLUDED.dispersion_relation_discussed,
                eigenfunction_computed = EXCLUDED.eigenfunction_computed,
                mode_identification_method = EXCLUDED.mode_identification_method,
                numerical_values = EXCLUDED.numerical_values,
                solar_cycle_phase = EXCLUDED.solar_cycle_phase,
                cycle_dependence = EXCLUDED.cycle_dependence,
                solar_activity_level = EXCLUDED.solar_activity_level,
                magnetic_field_considered = EXCLUDED.magnetic_field_considered,
                time_period = EXCLUDED.time_period,
                agrees_with_theory = EXCLUDED.agrees_with_theory,
                theoretical_prediction_tested = EXCLUDED.theoretical_prediction_tested,
                confirms_previous_work = EXCLUDED.confirms_previous_work,
                contradicts_previous_work = EXCLUDED.contradicts_previous_work,
                open_questions = EXCLUDED.open_questions,
                researcher_summary = EXCLUDED.researcher_summary,
                extraction_notes = EXCLUDED.extraction_notes,
                raw_response = EXCLUDED.raw_response,
                extracted_at = EXCLUDED.extracted_at,
                prompt_version = EXCLUDED.prompt_version,
                confidence = EXCLUDED.confidence,
                data_gaps = EXCLUDED.data_gaps
            """,
            identifier,
            result.get("central_contribution"),
            result.get("relevance_to_solar_inertial_modes"),
            result.get("data_type"),
            json.dumps(result.get("methods", [])),
            json.dumps(result.get("key_findings", [])),
            json.dumps(result.get("instruments", [])),
            json.dumps(result.get("wave_types", [])),
            json.dumps(result.get("solar_region", [])),
            json.dumps(result.get("azimuthal_orders", [])),
            json.dumps(result.get("physical_parameters", [])),
            json.dumps(result.get("measured_quantities", [])),
            json.dumps(result.get("constrained_quantities", [])),
            json.dumps(result.get("theoretical_framework", [])),
            result.get("detection_method"),
            result.get("observational_technique"),
            result.get("depth_range"),
            result.get("radial_order"),
            result.get("dispersion_relation_discussed"),
            result.get("eigenfunction_computed"),
            result.get("mode_identification_method"),
            json.dumps(result.get("numerical_values", [])),
            result.get("solar_cycle_phase"),
            result.get("cycle_dependence"),
            result.get("solar_activity_level"),
            result.get("magnetic_field_considered"),
            result.get("time_period"),
            result.get("agrees_with_theory"),
            result.get("theoretical_prediction_tested"),
            json.dumps(result.get("confirms_previous_work", [])),
            json.dumps(result.get("contradicts_previous_work", [])),
            json.dumps(result.get("open_questions", [])),
            result.get("researcher_summary"),
            result.get("extraction_notes"),
            raw_response,
            datetime.now(timezone.utc),
            prompt_version,
            result.get("confidence"),
            json.dumps(result.get("data_gaps", [])),
        )


async def extract_abstract(
    identifier: str, title: str, abstract: str
) -> tuple[dict, str]:
    """Sends a paper abstract to Claude and returns a structured extraction.
    Uses the tool_use API so Claude is forced through the typed schema defined
    in extraction_tool_schema. This is more reliable than asking Claude to return
    JSON as free text because it cannot drift into prose and the result arrives as
    a Python dict with no parsing needed. The function does not save to Postgres.
    The caller is responsible for calling save_extraction() with the returned result.
    This keeps the Claude call and the database write separated and independently
    testable.

    Args:
        identifier: ADS bibcode or arXiv ID of the paper. Used only for
                        structured logging. Not sent to Claude.

        title: Full paper title. Injected into the user message inside
                    <title> XML tags.

        abstract: Full abstract text. Injected into the user message
                    inside <abstract> XML tags.

    Returns:

        A tuple of:

            result: The structured extraction as a Python dict. Keys match the columns
                        of the extractions table. Returned directly from Claude's
                        tool_use block. No json.loads needed.

            raw_response: JSON-serialised copy of result as a string, stored in the
                            raw_response debug column in Postgres. Inspect this column
                            if an extraction looks wrong.

    Raises:

        httpx.HTTPStatusError: If the Anthropic API returns a non-2xx response, e.g. 401
                                for a bad API key or 529 for overload.

        StopIteration: If Claude's response contains no tool_use block, which should not
                        happen when tool_choice forces a specific tool but would surface
                        here rather than silently returning wrong data.
    """
    log = logger.bind(identifier=identifier)
    log.info("extraction_started")

    user_message = f"<title>\n{title}\n</title>\n\n<abstract>\n{abstract}\n</abstract>"

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": _get_api_key(),
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 2000,
                "system": system_prompt,
                "tools": [extraction_tool_schema],
                "tool_choice": {"type": "tool", "name": "extract_paper_metadata"},
                "messages": [{"role": "user", "content": user_message}],
            },
        )

    response.raise_for_status()
    data = response.json()

    # Find the tool_use block by type rather than assuming position.
    # tool_choice forces Claude to call the tool so this will always find a match
    # but next() raises StopIteration rather than returning wrong data if something unexpected comes back.
    tool_block = next(block for block in data["content"] if block["type"] == "tool_use")
    result = tool_block["input"]

    raw_response = json.dumps(result)

    log.info(
        "extraction_complete",
        data_type=result.get("data_type"),
        relevance=result.get("relevance_to_solar_inertial_modes"),
    )
    return result, raw_response


def _get_api_key() -> str:
    """Returns the Anthropic API key from application settings. Imported lazily
    inside the function to avoid a circular import between config and services
    at module load time.

    Returns:
        The Anthropic API key string from app.config.settings.
    """
    from app.config import settings

    return settings.anthropic_api_key
