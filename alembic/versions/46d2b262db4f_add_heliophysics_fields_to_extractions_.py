"""add heliophysics fields to extractions table

Revision ID: 46d2b262db4f
Revises: ccb4dd3e63a8
Create Date: 2026-04-11 13:05:11.659646

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '46d2b262db4f'
down_revision: Union[str, Sequence[str], None] = 'ccb4dd3e63a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade():
    op.add_column("extractions", sa.Column("wave_types", sa.JSON(), nullable=True))
    op.add_column("extractions", sa.Column("solar_region", sa.JSON(), nullable=True))
    op.add_column("extractions", sa.Column("azimuthal_orders", sa.JSON(), nullable=True))
    op.add_column("extractions", sa.Column("extraction_notes", sa.Text(), nullable=True))
    op.add_column("extractions", sa.Column("raw_response", sa.Text(), nullable=True))
    op.add_column("extractions", sa.Column("time_period", sa.String(), nullable=True))
    op.add_column("extractions", sa.Column("theoretical_framework", sa.JSON(), nullable=True))
    op.add_column("extractions", sa.Column("open_questions", sa.JSON(), nullable=True))
    op.add_column("extractions", sa.Column("numerical_values", sa.JSON(), nullable=True))
    op.add_column("extractions", sa.Column("central_contribution", sa.Text(), nullable=True))
    op.add_column("extractions", sa.Column("relevance_to_solar_inertial_modes", sa.String(), nullable=True))
    op.add_column("extractions", sa.Column("physical_parameters", sa.JSON(), nullable=True))
    op.add_column("extractions", sa.Column("solar_cycle_phase", sa.String(), nullable=True))
    op.add_column("extractions", sa.Column("cycle_dependence", sa.String(), nullable=True))
    op.add_column("extractions", sa.Column("detection_method", sa.Text(), nullable=True))
    op.add_column("extractions", sa.Column("observational_technique", sa.String(), nullable=True))
    op.add_column("extractions", sa.Column("measured_quantities", sa.JSON(), nullable=True))
    op.add_column("extractions", sa.Column("constrained_quantities", sa.JSON(), nullable=True))
    op.add_column("extractions", sa.Column("depth_range", sa.String(), nullable=True))
    op.add_column("extractions", sa.Column("radial_order", sa.String(), nullable=True))
    op.add_column("extractions", sa.Column("dispersion_relation_discussed", sa.String(), nullable=True))
    op.add_column("extractions", sa.Column("eigenfunction_computed", sa.String(), nullable=True))
    op.add_column("extractions", sa.Column("mode_identification_method", sa.Text(), nullable=True))
    op.add_column("extractions", sa.Column("agrees_with_theory", sa.String(), nullable=True))
    op.add_column("extractions", sa.Column("theoretical_prediction_tested", sa.Text(), nullable=True))
    op.add_column("extractions", sa.Column("confirms_previous_work", sa.JSON(), nullable=True))
    op.add_column("extractions", sa.Column("contradicts_previous_work", sa.JSON(), nullable=True))
    op.add_column("extractions", sa.Column("solar_activity_level", sa.String(), nullable=True))
    op.add_column("extractions", sa.Column("magnetic_field_considered", sa.String(), nullable=True))
    op.add_column("extractions", sa.Column("researcher_summary", sa.Text(), nullable=True))

def downgrade():
    op.drop_column("extractions", "wave_types")
    op.drop_column("extractions", "solar_region")
    op.drop_column("extractions", "azimuthal_orders")
    op.drop_column("extractions", "extraction_notes")
    op.drop_column("extractions", "raw_response")
    op.drop_column("extractions", "time_period")
    op.drop_column("extractions", "theoretical_framework")
    op.drop_column("extractions", "open_questions")
    op.drop_column("extractions", "numerical_values")
    op.drop_column("extractions", "central_contribution")
    op.drop_column("extractions", "relevance_to_solar_inertial_modes")
    op.drop_column("extractions", "physical_parameters")
    op.drop_column("extractions", "solar_cycle_phase")
    op.drop_column("extractions", "cycle_dependence")
    op.drop_column("extractions", "detection_method")
    op.drop_column("extractions", "observational_technique")
    op.drop_column("extractions", "measured_quantities")
    op.drop_column("extractions", "constrained_quantities")
    op.drop_column("extractions", "depth_range")
    op.drop_column("extractions", "radial_order")
    op.drop_column("extractions", "dispersion_relation_discussed")
    op.drop_column("extractions", "eigenfunction_computed")
    op.drop_column("extractions", "mode_identification_method")
    op.drop_column("extractions", "agrees_with_theory")
    op.drop_column("extractions", "theoretical_prediction_tested")
    op.drop_column("extractions", "confirms_previous_work")
    op.drop_column("extractions", "contradicts_previous_work")
    op.drop_column("extractions", "solar_activity_level")
    op.drop_column("extractions", "magnetic_field_considered")
    op.drop_column("extractions", "researcher_summary")