"""Run i006/i007/i008 on the UK snapshot and report flags."""

import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import django  # noqa: E402

django.setup()

from uncorrupt.indicators.catalog.i006_incorporation_proximity import (  # noqa: E402
    IncorporationProximity,
)
from uncorrupt.indicators.catalog.i007_value_vs_company_size import (  # noqa: E402
    ValueVsCompanySize,
)
from uncorrupt.indicators.catalog.i008_dormancy_delinquency import (  # noqa: E402
    DormancyDelinquency,
)
from uncorrupt.indicators.context import EvaluationContext  # noqa: E402

ctx = EvaluationContext(source_id="uk_contracts_finder", locale="gb")

for cls in [IncorporationProximity, ValueVsCompanySize, DormancyDelinquency]:
    ind = cls()
    flags = list(ind.evaluate(ctx))
    print(f"{ind.id}: {len(flags)} flags, {ind.units_evaluated} units evaluated")
    for f in flags[:5]:
        print(f"  {f.subject_ref}: {f.explanation[:200]}")
    if len(flags) > 5:
        print(f"  ... and {len(flags) - 5} more")
    print()
