"""Regenerate the commodity seed from the committed PDF fixtures.

Run from the repo root::

    uv run python scripts/generate_commodity_seed.py

Writes `src/presyowatch/data/commodities.csv` and `commodity_aliases.csv`.

**The seeding policy, and why it is deliberately incomplete.**

A commodity's identity is the triple ``(group, commodity, specification)``. It cannot be the
name alone: seven rice varieties — Premium, Basmati, Glutinous and friends — appear under
both IMPORTED and LOCAL COMMERCIAL RICE and are different products. Nor can the
specification be dropped, because Corn Grits comes in Feed Grade, White Food Grade and
Yellow Food Grade and nothing else distinguishes them.

But the specification is also the *least* reliable field. Multi-line specification cells
extract with their lines out of order or with text leaked in from the row above:
``'pcs/kg) Male, Medium (12-14'`` for what another sheet renders as
``'Male, Medium (12-14 pcs/kg)'``. Seventeen `(group, commodity)` pairs disagree between
sheets this way.

So this script seeds only the triples attested by **more than half** the fixture sheets.
Agreement across markets and provinces is decent evidence that a triple is what the source
publishes rather than an extraction artefact, and the artefacts are exactly the poorly
attested ones — ``'Male, Medium (12-14 pcs/kg)'`` appears on three sheets while
``'pcs/kg) Male, Medium (12-14'`` appears on one.

Requiring *every* sheet was the first attempt and was too strict: it threw away well-attested
commodities because a single sheet had mangled their specification.

Triples below the threshold are left out on purpose. At runtime they will not resolve, and an
unresolved row is quarantined with its raw strings recorded, which is exactly where a human
should look to decide whether it is a new commodity or a mangled copy of one already seeded.
Note that the threshold does not *merge* anything — each seeded triple stays its own
commodity. It only decides what is well enough attested to seed unattended.

Guessing here would be the expensive kind of wrong. Merging
``'Male, Medium (12-14'`` into ``'Male, Medium (12-14 pcs/kg)'`` is obvious to a person and
plausible to a prefix heuristic — but the same heuristic would merge Corn Grits Feed Grade
into Corn Grits Food Grade, silently averaging animal feed into the price of food.
"""

import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from presyowatch.commodities import alias_key
from presyowatch.sources.bantay_presyo import ParsedSheet, SheetParseError, parse_sheet

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "pdf"
DATA = ROOT / "src" / "presyowatch" / "data"

SLUG_MAX = 120
PREVIEW_LIMIT = 15
"""How many unseeded triples to list before summarising the rest."""
_NON_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(*parts: str) -> str:
    joined = " ".join(part for part in parts if part)
    slug = _NON_SLUG.sub("-", joined.lower()).strip("-")
    return slug[:SLUG_MAX].rstrip("-")


def read_fixtures() -> dict[str, ParsedSheet]:
    """Parse every committed fixture, reporting and skipping the ones that cannot be read.

    The corpus deliberately includes sheets the parser rejects, so that the failure is
    exercised. They contribute no vocabulary and must not count towards the attestation
    threshold, which is a fraction of the sheets actually read.
    """
    sheets: dict[str, ParsedSheet] = {}
    for path in sorted(FIXTURES.glob("*.pdf")):
        try:
            sheets[path.name] = parse_sheet(path.read_bytes())
        except SheetParseError as exc:
            print(f"  - skipped {path.name}: {exc.reason}")
    return sheets


def main() -> int:
    sheets = read_fixtures()
    if not sheets:
        print("no readable fixtures found", file=sys.stderr)
        return 1

    seen_in: dict[tuple[str, str, str | None], set[str]] = defaultdict(set)
    units: dict[tuple[str, str, str | None], set[str]] = defaultdict(set)
    for name, sheet in sheets.items():
        for row in sheet.rows:
            triple = (row.group, row.commodity, row.specification)
            seen_in[triple].add(name)
            units[triple].add(row.unit)

    def in_order(triple: tuple[str, str, str | None]) -> tuple[str, str, str]:
        return (triple[0], triple[1], triple[2] or "")

    # Attested by more than half the sheets. See the module docstring for why not "all".
    threshold = len(sheets) // 2 + 1
    stable = sorted(
        (triple for triple, names in seen_in.items() if len(names) >= threshold),
        key=in_order,
    )
    skipped = sorted(
        (triple for triple, names in seen_in.items() if len(names) < threshold),
        key=in_order,
    )
    print(f"attestation threshold : {threshold} of {len(sheets)} sheets")

    DATA.mkdir(parents=True, exist_ok=True)
    slugs: dict[tuple[str, str, str | None], str] = {}
    used: set[str] = set()
    for group, commodity, specification in stable:
        slug = slugify(group, commodity, specification or "")
        suffix = 2
        while slug in used:
            slug = f"{slugify(group, commodity, specification or '')[: SLUG_MAX - 3]}-{suffix}"
            suffix += 1
        used.add(slug)
        slugs[(group, commodity, specification)] = slug

    commodities_path = DATA / "commodities.csv"
    with commodities_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["canonical_slug", "group", "name", "specification", "unit"])
        for triple in stable:
            group, commodity, specification = triple
            unit_values = sorted(units[triple])
            if len(unit_values) > 1:
                print(f"  ! {triple} has several units {unit_values}; taking the first")
            writer.writerow([slugs[triple], group, commodity, specification or "", unit_values[0]])

    aliases_path = DATA / "commodity_aliases.csv"
    with aliases_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["alias_key", "canonical_slug"])
        for triple in stable:
            group, commodity, specification = triple
            writer.writerow([alias_key(group, commodity, specification), slugs[triple]])

    print(f"sheets read           : {len(sheets)}")
    print(f"distinct triples      : {len(seen_in)}")
    print(f"seeded                : {len(stable)}")
    print(f"left for curation     : {len(skipped)}")
    print(f"wrote {commodities_path.relative_to(ROOT)}")
    print(f"wrote {aliases_path.relative_to(ROOT)}")
    print("\nleft unseeded, will quarantine at runtime until a human maps them:")
    for group, commodity, specification in skipped[:PREVIEW_LIMIT]:
        sheets_with = len(seen_in[(group, commodity, specification)])
        print(f"  [{sheets_with}/{len(sheets)}] {group} | {commodity} | {specification}")
    if len(skipped) > PREVIEW_LIMIT:
        print(f"  ... and {len(skipped) - PREVIEW_LIMIT} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
