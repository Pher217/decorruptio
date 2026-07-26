"""Re-resolve GLEIF-LEI entities under the Companies-House-RA-gated rule.

The original GLEIF ingest cross-linked any GB-jurisdiction record to
staging.Company purely on `entity.jurisdiction == "GB"`, without checking
which registration authority `entity.registeredAs` actually came from. That
produced false links for GB records registered with a non-Companies-House
authority (FCA, Charity Commission, Pensions Regulator, GLEIF's
"authority not on the list" placeholders, ...) whose number happened to pad
to a real, unrelated company number.

This command re-derives `company_number` for every existing GLEIF-LEI
Entity from the jurisdiction/authority/number already stored in its
`properties` (captured at ingest time, so no re-fetch is needed), using the
corrected `uncorrupt.graph.gleif._resolve_gb_company` gate. It never deletes
an Entity — only clears `company_number` where the existing link is not
authority-validated, and never invents a link that the new gate wouldn't
also produce from a fresh ingest.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from uncorrupt.graph.gleif import REGISTRY_SCHEME, _resolve_gb_company
from uncorrupt.graph.models import Entity


class Command(BaseCommand):
    help = "Re-resolve GLEIF-LEI Entity.company_number under the Companies-House-RA-gated rule."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing to the database.",
        )

    def handle(self, *args, dry_run: bool = False, **options):
        entities = Entity.objects.filter(entity_type="company", registry_scheme=REGISTRY_SCHEME)

        checked = 0
        links_removed = 0
        links_added = 0
        unchanged = 0
        to_update: list[Entity] = []

        for entity in entities.iterator():
            checked += 1
            properties = entity.properties or {}
            jurisdiction = properties.get("jurisdiction") or ""
            registered_at = properties.get("local_registration_authority")
            registered_as = properties.get("local_registration_number")

            company = _resolve_gb_company(jurisdiction, registered_at, registered_as)
            correct_number = company.company_number if company else None

            if correct_number == entity.company_number:
                unchanged += 1
                continue

            if entity.company_number and not correct_number:
                links_removed += 1
            elif correct_number and not entity.company_number:
                links_added += 1

            entity.company_number = correct_number
            to_update.append(entity)

        if not dry_run and to_update:
            with transaction.atomic():
                Entity.objects.bulk_update(to_update, ["company_number"])

        mode = "DRY RUN — " if dry_run else ""
        self.stdout.write(
            f"{mode}Checked {checked} GLEIF-LEI entities: "
            f"{links_removed} false links removed, "
            f"{links_added} links added, "
            f"{unchanged} unchanged."
        )
