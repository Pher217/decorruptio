"""Add source_id to staging.Flag so deletes/counts can be scoped per source.

`source_id` is declared exactly like the other staging models
(`CharField(max_length=50, db_index=True)`), with a one-time default for rows
that already exist: at the time this migration is written only
`uk_contracts_finder` has ever been scored, so every pre-existing Flag row is
backfilled with that source_id. New rows are created by
`scripts/run_indicators.py`, which always sets source_id from the run.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("staging", "0007_backfill_award_value_kind"),
    ]

    operations = [
        # Add the column with a one-time default that backfills every pre-existing
        # Flag row. Only `uk_contracts_finder` has ever been scored, so that source_id
        # is the correct value for all existing rows.
        migrations.AddField(
            model_name="flag",
            name="source_id",
            field=models.CharField(db_index=True, default="uk_contracts_finder", max_length=50),
        ),
        # Remove the database default so new rows must explicitly provide source_id,
        # matching the declaration in models.py and the other staging tables.
        migrations.AlterField(
            model_name="flag",
            name="source_id",
            field=models.CharField(db_index=True, max_length=50),
        ),
    ]
