"""One non-cancelled appointment per doctor and exact timestamp.

Revision ID: c4e82a19bd70
Revises: 7d2a61f4c9be
"""
from alembic import op
import sqlalchemy as sa

revision = 'c4e82a19bd70'
down_revision = '7d2a61f4c9be'
branch_labels = None
depends_on = None


def upgrade():
    # Keep duplicate preflight and index creation in one transaction, without a race.
    op.execute('LOCK TABLE appointments IN SHARE MODE')
    duplicate = op.get_bind().execute(sa.text("""
        SELECT 1 FROM appointments WHERE status <> 'cancelled'
        GROUP BY doctor_id, appointment_time HAVING count(*) > 1 LIMIT 1
    """)).first()
    if duplicate:
        raise RuntimeError('Duplicate active doctor/time slots; resolve explicitly before migration')
    # The frozen initial revision creates current model metadata on a fresh DB.
    op.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_appointments_doctor_time_active
                  ON appointments (doctor_id, appointment_time) WHERE status <> 'cancelled'""")


def downgrade():
    op.drop_index('uq_appointments_doctor_time_active', table_name='appointments')
