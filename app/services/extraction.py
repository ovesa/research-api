import json
from datetime import datetime, timezone

import httpx
import structlog

from app.services.database import get_pool

logger = structlog.get_logger(__name__)


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


async def save_extraction(identifier: str, result: dict, raw_response: str) -> None:
    """Save an extraction result to Postgres.

    Args:
        identifier (str): ADS bibcode or arXiv ID of the paper.
        result (dict): Structured extraction from Claude.
        raw_response (str): Raw Claude response for debugging.
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
                extracted_at
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11, $12, $13, $14, $15, $16, $17, $18, $19, $20,
                $21, $22, $23, $24, $25, $26, $27, $28, $29, $30,
                $31, $32, $33, $34, $35, $36
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
                extracted_at = EXCLUDED.extracted_at
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
        )


async def extract_abstract(
    identifier: str, title: str, abstract: str
) -> tuple[dict, str]:
    """Send a paper abstract to Claude and return structured extraction.

    Args:
        identifier (str): Paper identifier for logging.
        title (str): Paper title.
        abstract (str): Paper abstract text.

    Returns:
        tuple[dict, str]: Parsed extraction dict and raw Claude response.
    """
    log = logger.bind(identifier=identifier)
    log.info("extraction_started")

    prompt = f"""You are Dr. HelioBot, a senior heliophysics researcher with over 30 years \
experience studying solar interior dynamics, inertial modes, and Rossby waves. You have deep \
expertise in helioseismology, MHD theory, solar dynamo, and analyzing data from instruments \
like SDO/HMI, GONG, and MDI. You are intimately familiar with the work of Gizon, Löptien, \
Hanasoge, Dikpati, Proxauf, Hathaway, and others in this field. You know the difference between \
equatorial Rossby modes, high-latitude inertial modes, thermal Rossby waves, and columnar \
convective modes. You have a keen eye for extracting the most relevent information from papers.

Your task is to carefully read this paper and extract structured research metadata that will \
be used to build a literature database for solar inertial mode research.

First, identify what type of paper this is and what its central contribution is. Then extract \
the following fields precisely and conservatively based ONLY on information stated in the \
abstract or title. Never invent information not present in the text.

Title: {title}

Abstract: {abstract}

Return ONLY a valid JSON object. Use empty lists [] for list fields and empty strings "" \
for string fields when information is not present. Never guess or invent.

{{
  "central_contribution": "one sentence summarizing the paper's main contribution to the field",

  "relevance_to_solar_inertial_modes": "one of: primary (inertial modes or rossby waves in the Sun are the main subject), secondary (studies related solar dynamics that directly informs inertial mode research), peripheral (mentions inertial modes briefly but is primarily about something else)",

  "data_type": "one of: observational (uses real solar data), theoretical (analytical only), computational (numerical simulations), review (summarizes literature), mixed (combines multiple approaches)",

  "methods": ["specific methods or techniques used"],

  "key_findings": [
    {{
      "finding": "description of the finding",
      "type": "one of: detection, measurement, constraint, theoretical, null_result",
      "confidence": "one of: definitive, tentative, marginal"
    }}
  ],

  "instruments": ["instruments or datasets explicitly mentioned, e.g. SDO, HMI, Solar Dynamics Observatory, Helioseismic and Magnetic Imager, GONG, MDI, Hinode"],

  "wave_types": ["types of waves or modes studied, e.g. inertial modes, equatorial rossby waves, high-latitude inertial modes, thermal rossby waves, columnar convective modes, gravito-inertial waves, g-modes, f-modes, p-modes"],

  "solar_region": ["solar regions studied, e.g. convection zone, tachocline, photosphere, radiative interior, polar region, equatorial region"],

  "azimuthal_orders": ["m values if mentioned, e.g. m=1, m=6 to m=10, empty if not mentioned"],

  "physical_parameters": ["physical quantities studied, e.g. differential rotation, meridional flow, Reynolds stress, eigenfrequency, phase velocity, superadiabaticity"],

  "measured_quantities": ["quantities directly measured from data, e.g. mode frequency, phase velocity, mode lifetime, power spectrum, velocity amplitude, eigenfunction shape"],

  "constrained_quantities": ["quantities inferred or constrained by results, e.g. superadiabaticity, turbulent viscosity, convective velocity, differential rotation profile, meridional flow speed"],

  "theoretical_framework": ["physical models or approaches used, e.g. shallow water model, MHD, linear perturbation theory, anelastic approximation, Boussinesq approximation, quasi-geostrophic theory, normal mode coupling"],

  "detection_method": "how modes or waves were detected or measured, e.g. ring-diagram analysis, time-distance helioseismology, local correlation tracking, Doppler velocity maps, power spectrum analysis, cross-covariance functions, normal mode coupling, empty string if not applicable",

  "observational_technique": "one of: helioseismology, spectroscopy, imaging, magnetogram, dopplergram, photometry, simulation_only, not_applicable",

  "depth_range": "depth range studied if mentioned, e.g. 0-30 Mm, upper convection zone, near surface, full convection zone depth, empty string if not mentioned",

  "radial_order": "radial order n if mentioned, e.g. n=0, n=1, sectoral modes, empty string if not mentioned",

  "dispersion_relation_discussed": "yes or no — whether the paper derives or discusses the dispersion relation of the modes",

  "eigenfunction_computed": "yes or no — whether eigenfunctions are computed or observed",

  "mode_identification_method": "how modes are identified, e.g. frequency matching, eigenfunction comparison, power spectrum peaks, empty string if not applicable",

  "numerical_values": [
    {{"quantity": "name of quantity", "value": "numeric value", "unit": "unit"}}
  ],

  "solar_cycle_phase": "solar cycle context if mentioned, e.g. solar minimum, cycle 24, cycles 23-25, empty string if not mentioned",

  "cycle_dependence": "one of: yes, no, partial, not_mentioned",

  "solar_activity_level": "one of: solar_minimum, solar_maximum, rising_phase, declining_phase, multiple_cycles, not_mentioned",

  "magnetic_field_considered": "yes or no — whether magnetic field effects on the modes are discussed",

  "time_period": "observational time range if mentioned, format as YYYY-YYYY or duration e.g. 14 years, empty string if not mentioned",

  "agrees_with_theory": "one of: yes, no, partial, not_applicable — whether observational results agree with theoretical predictions",

  "theoretical_prediction_tested": "which theoretical prediction was tested if any, e.g. Matsuno-Gill model, linear wave theory, gyroscopic pumping, empty string if not applicable",

  "confirms_previous_work": ["previous results this paper confirms or extends, e.g. confirms Löptien et al. 2018 detection"],

  "contradicts_previous_work": ["previous results this paper disputes or revises, empty list if none"],

  "open_questions": ["unresolved questions explicitly identified, including phrases like remains unclear, future observations needed, not yet understood, warrants further study"],

  "researcher_summary": "As a heliophysics expert, write 2-3 sentences explaining why this paper matters for solar inertial mode research, what gap it fills, and what a researcher studying inertial modes should take away from it",

  "extraction_notes": "note any ambiguity or limitation in this extraction, empty string if clear"
}}

Return only the JSON object. No explanation before or after. No markdown. No code blocks."""

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
                "messages": [{"role": "user", "content": prompt}],
            },
        )

    response.raise_for_status()
    data = response.json()
    raw_text = data["content"][0]["text"].strip()

    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError:
        clean = raw_text.replace("```json", "").replace("```", "").strip()
        result = json.loads(clean)

    log.info(
        "extraction_complete",
        data_type=result.get("data_type"),
        relevance=result.get("relevance_to_solar_inertial_modes"),
    )
    return result, raw_text


def _get_api_key() -> str:
    """Get the Anthropic API key from settings."""
    from app.config import settings

    return settings.anthropic_api_key
