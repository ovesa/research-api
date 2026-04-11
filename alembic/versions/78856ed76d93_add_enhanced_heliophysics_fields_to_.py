"""add enhanced heliophysics fields to extractions

Revision ID: 78856ed76d93
Revises: 46d2b262db4f
Create Date: 2026-04-11 13:32:52.279300

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '78856ed76d93'
down_revision: Union[str, Sequence[str], None] = '46d2b262db4f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.add_column("extractions", sa.Column("central_contribution", sa.Text(), nullable=True))
    op.add_column("extractions", sa.Column("relevance_to_solar_inertial_modes", sa.String(), nullable=True))
    op.add_column("extractions", sa.Column("physical_parameters", sa.JSON(), nullable=True))
    op.add_column("extractions", sa.Column("measured_quantities", sa.JSON(), nullable=True))
    op.add_column("extractions", sa.Column("constrained_quantities", sa.JSON(), nullable=True))
    op.add_column("extractions", sa.Column("detection_method", sa.Text(), nullable=True))
    op.add_column("extractions", sa.Column("observational_technique", sa.String(), nullable=True))
    op.add_column("extractions", sa.Column("depth_range", sa.String(), nullable=True))
    op.add_column("extractions", sa.Column("radial_order", sa.String(), nullable=True))
    op.add_column("extractions", sa.Column("dispersion_relation_discussed", sa.String(), nullable=True))
    op.add_column("extractions", sa.Column("eigenfunction_computed", sa.String(), nullable=True))
    op.add_column("extractions", sa.Column("mode_identification_method", sa.Text(), nullable=True))
    op.add_column("extractions", sa.Column("solar_cycle_phase", sa.String(), nullable=True))
    op.add_column("extractions", sa.Column("cycle_dependence", sa.String(), nullable=True))
    op.add_column("extractions", sa.Column("magnetic_field_considered", sa.String(), nullable=True))
    op.add_column("extractions", sa.Column("agrees_with_theory", sa.String(), nullable=True))
    op.add_column("extractions", sa.Column("theoretical_prediction_tested", sa.Text(), nullable=True))
    op.add_column("extractions", sa.Column("confirms_previous_work", sa.JSON(), nullable=True))
    op.add_column("extractions", sa.Column("contradicts_previous_work", sa.JSON(), nullable=True))
    op.add_column("extractions", sa.Column("solar_activity_level", sa.String(), nullable=True))
    op.add_column("extractions", sa.Column("researcher_summary", sa.Text(), nullable=True))

def downgrade():
    op.drop_column("extractions", "central_contribution")
    op.drop_column("extractions", "relevance_to_solar_inertial_modes")
    op.drop_column("extractions", "physical_parameters")
    op.drop_column("extractions", "measured_quantities")
    op.drop_column("extractions", "constrained_quantities")
    op.drop_column("extractions", "detection_method")
    op.drop_column("extractions", "observational_technique")
    op.drop_column("extractions", "depth_range")
    op.drop_column("extractions", "radial_order")
    op.drop_column("extractions", "dispersion_relation_discussed")
    op.drop_column("extractions", "eigenfunction_computed")
    op.drop_column("extractions", "mode_identification_method")
    op.drop_column("extractions", "solar_cycle_phase")
    op.drop_column("extractions", "cycle_dependence")
    op.drop_column("extractions", "solar_activity_level")
    op.drop_column("extractions", "magnetic_field_considered")
    op.drop_column("extractions", "agrees_with_theory")
    op.drop_column("extractions", "theoretical_prediction_tested")
    op.drop_column("extractions", "confirms_previous_work")
    op.drop_column("extractions", "contradicts_previous_work")
    op.drop_column("extractions", "researcher_summary")