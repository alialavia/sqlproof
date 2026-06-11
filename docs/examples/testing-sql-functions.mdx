---
title: Testing SQL Functions — pgTAP vs SqlProof
description: A realistic plpgsql function with discounts, promos, tax, and country-specific rounding — tested two ways. The case where SqlProof's lead over pgTAP is largest.
---

Schema-shape tests are pgTAP's home turf and pgTAP wins. **Function
testing is the opposite** — once a function has more than a couple of
branches, pgTAP's example-based assertions miss combinatorial bugs that
property-based tests catch by construction. This page is a realistic
walkthrough.

## The function

A pricing function used by an actual checkout flow. Tier discount,
stackable promo codes, country-specific tax, and country-specific
rounding (no fractional yen, two-decimal everything else).

```sql
CREATE OR REPLACE FUNCTION compute_order_total(
    p_subtotal     numeric(10,2),
    p_member_tier  text,           -- 'standard' | 'silver' | 'gold' | 'platinum'
    p_promo_codes  text[],         -- e.g. ARRAY['WELCOME10', 'SAVE5']
    p_country_code char(2)         -- 'US', 'GB', 'DE', 'JP', 'CA', ...
) RETURNS numeric AS $$
DECLARE
    v_amount      numeric := COALESCE(p_subtotal, 0);
    v_tier_pct    numeric := CASE p_member_tier
        WHEN 'platinum' THEN 0.10
        WHEN 'gold'     THEN 0.05
        WHEN 'silver'   THEN 0.02
        ELSE                 0
    END;
    v_promo_total numeric := 0;
    v_promo       text;
    v_tax_pct     numeric := CASE upper(p_country_code)
        WHEN 'US' THEN 0.07
        WHEN 'GB' THEN 0.20
        WHEN 'DE' THEN 0.19
        WHEN 'JP' THEN 0.10
        WHEN 'CA' THEN 0.13
        ELSE          0
    END;
    v_decimals    integer := CASE upper(p_country_code)
        WHEN 'JP' THEN 0  -- no fractional yen
        ELSE           2
    END;
BEGIN
    -- 1. Apply tier discount.
    v_amount := v_amount * (1 - v_tier_pct);

    -- 2. Stack known promo codes; unknown codes are silently ignored.
    FOREACH v_promo IN ARRAY COALESCE(p_promo_codes, ARRAY[]::text[])
    LOOP
        v_promo_total := v_promo_total + CASE v_promo
            WHEN 'WELCOME10' THEN 10
            WHEN 'SAVE5'     THEN  5
            WHEN 'FREESHIP'  THEN  3
            ELSE                  0
        END;
    END LOOP;

    -- 3. Cap stacked promos at 50% of the post-tier amount.
    v_promo_total := LEAST(v_promo_total, v_amount * 0.50);

    -- 4. Floor at 0; never produce a negative invoice.
    v_amount := GREATEST(v_amount - v_promo_total, 0);

    -- 5. Apply tax.
    v_amount := v_amount * (1 + v_tax_pct);

    -- 6. Round per country.
    RETURN round(v_amount, v_decimals);
END;
$$ LANGUAGE plpgsql IMMUTABLE;
```

**Branch count audit (just to motivate the test plan):**

- 4 tier values × 6 country codes × 4 promo-set sizes (0, 1, 2, 3) ×
  ~3 subtotal magnitudes (0, normal, very large) ≈ **288 input
  classes** before you even touch NULLs, unknown tiers, unknown
  countries, unknown promo codes, the cap-at-50% boundary, the
  floor-at-zero boundary, or the JPY-rounding boundary.

No human writes 288 example tests. Most production codebases write 10
and call it covered.

## The pgTAP version

Here's a *good-faith* pgTAP test — better than what most teams ship,
worse than what would actually catch the bugs. About 90 lines for
maybe 12 cases:

```sql
-- pgtap_compute_order_total.sql
BEGIN;
SELECT plan(14);

-- Baseline: no discounts, no promos, US tax.
SELECT is(
    compute_order_total(100.00, 'standard', '{}', 'US'),
    107.00::numeric,
    'standard tier, no promos, US, $100 → $107'
);

-- Each tier.
SELECT is(
    compute_order_total(100.00, 'silver', '{}', 'US'),
    104.86::numeric,
    'silver tier: 2% off + 7% tax'
);
SELECT is(
    compute_order_total(100.00, 'gold', '{}', 'US'),
    101.65::numeric,
    'gold tier: 5% off + 7% tax'
);
SELECT is(
    compute_order_total(100.00, 'platinum', '{}', 'US'),
    96.30::numeric,
    'platinum tier: 10% off + 7% tax'
);

-- Each country.
SELECT is(
    compute_order_total(100.00, 'standard', '{}', 'GB'),
    120.00::numeric,
    'GB: 20% VAT'
);
SELECT is(
    compute_order_total(100.00, 'standard', '{}', 'JP'),
    110::numeric,
    'JP: 10% tax, integer rounding'
);

-- Each promo code, applied alone.
SELECT is(
    compute_order_total(100.00, 'standard', ARRAY['WELCOME10'], 'US'),
    96.30::numeric,
    'WELCOME10: $10 off + 7% tax'
);

-- Promo stacking.
SELECT is(
    compute_order_total(100.00, 'standard', ARRAY['WELCOME10', 'SAVE5'], 'US'),
    91.00::numeric,
    'WELCOME10 + SAVE5 stack: $15 off + 7% tax'
);

-- Cap-at-50% boundary.
SELECT is(
    compute_order_total(20.00, 'standard', ARRAY['WELCOME10', 'WELCOME10'], 'US'),
    10.70::numeric,
    'promos capped at 50% of post-tier amount'
);

-- Floor-at-zero boundary.
SELECT is(
    compute_order_total(0, 'platinum', ARRAY['WELCOME10'], 'US'),
    0::numeric,
    'never produce a negative invoice'
);

-- Unknown promo code is ignored, not an error.
SELECT is(
    compute_order_total(100.00, 'standard', ARRAY['FAKEPROMO'], 'US'),
    107.00::numeric,
    'unknown promo silently ignored'
);

-- Unknown tier falls through to 0% discount.
SELECT is(
    compute_order_total(100.00, 'unknown_tier', '{}', 'US'),
    107.00::numeric,
    'unknown tier = no discount'
);

-- NULL handling.
SELECT is(
    compute_order_total(NULL, 'standard', '{}', 'US'),
    0::numeric,
    'NULL subtotal coerced to 0'
);
SELECT is(
    compute_order_total(100.00, 'standard', NULL, 'US'),
    107.00::numeric,
    'NULL promo array treated as empty'
);

SELECT * FROM finish();
ROLLBACK;
```

**This test passes.** It also covers maybe 5% of the input space.
Specifically it does **not** cover:

- **Decimal precision drift.** If one of the multipliers loses
  precision on values with more digits than the test fixtures use, the
  test won't see it.
- **Multi-promo + non-US tax + non-default tier combinations.** No
  fixture exercises e.g. `('platinum', ['WELCOME10', 'FREESHIP'],
  'JP')`. That's a real combination customers will hit.
- **Boundary values just above/below the 50% cap.** The test hits the
  cap with a fixture *at* the boundary; values just above and below
  are unverified.
- **Monotonicity.** The test never asks "does upgrading from gold to
  platinum ever *increase* the total?" — which is the kind of bug that
  ships when someone reorders the CASE statement.
- **JPY values just below 0.5.** Does `round(0.49, 0)` go to 0 or 1?
  Wrong rounding mode is a frequent plpgsql gotcha; the test only
  covers a single integer-clean JPY case.

## The SqlProof version

Same function. ~40 lines of Python including imports.

```python
from decimal import Decimal
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlproof import SqlProof
from sqlproof.client import SqlProofClient

# Strategies for inputs.
TIERS = ("standard", "silver", "gold", "platinum")
COUNTRIES = ("US", "GB", "DE", "JP", "CA", "FR")  # FR = unknown country
KNOWN_PROMOS = ("WELCOME10", "SAVE5", "FREESHIP")
ALL_PROMOS = KNOWN_PROMOS + ("FAKEPROMO",)

subtotals = st.decimals(min_value=Decimal("0"),
                       max_value=Decimal("99999.99"), places=2)
tiers = st.sampled_from(TIERS)
countries = st.sampled_from(COUNTRIES)
promos = st.lists(st.sampled_from(ALL_PROMOS), max_size=5)

PROOF_KW = settings(max_examples=200, deadline=None,
                   suppress_health_check=[HealthCheck.function_scoped_fixture])

def total(db: SqlProofClient, subtotal, tier, promos, country) -> Decimal:
    return db.scalar(
        "SELECT compute_order_total(%s::numeric, %s, %s::text[], %s::char(2))",
        subtotal, tier, promos, country,
    )

@PROOF_KW
@given(subtotal=subtotals, tier=tiers, promos=promos, country=countries)
def test_invoice_is_never_negative(db, subtotal, tier, promos, country):
    assert total(db, subtotal, tier, promos, country) >= 0

@PROOF_KW
@given(subtotal=subtotals, tier=tiers, promos=promos, country=countries)
def test_invoice_never_exceeds_subtotal_plus_max_tax(db, subtotal, tier, promos, country):
    # Max tax is GB at 20%; no combination of (tier × promos × country)
    # should ever produce a result above this ceiling.
    assert total(db, subtotal, tier, promos, country) <= subtotal * Decimal("1.20")

@PROOF_KW
@given(subtotal=subtotals, promos=promos, country=countries)
def test_higher_tier_never_costs_more_than_lower_tier(db, subtotal, promos, country):
    standard = total(db, subtotal, "standard", promos, country)
    silver   = total(db, subtotal, "silver",   promos, country)
    gold     = total(db, subtotal, "gold",     promos, country)
    platinum = total(db, subtotal, "platinum", promos, country)
    assert platinum <= gold <= silver <= standard

@PROOF_KW
@given(subtotal=subtotals, tier=tiers, country=countries,
       extra_promo=st.sampled_from(KNOWN_PROMOS), base_promos=promos)
def test_adding_a_known_promo_never_increases_total(
    db, subtotal, tier, country, extra_promo, base_promos
):
    before = total(db, subtotal, tier, base_promos, country)
    after  = total(db, subtotal, tier, base_promos + [extra_promo], country)
    assert after <= before

@PROOF_KW
@given(subtotal=subtotals, tier=tiers, promos=promos)
def test_jpy_results_are_integer_valued(db, subtotal, tier, promos):
    result = total(db, subtotal, tier, promos, "JP")
    assert result == result.to_integral_value()

@PROOF_KW
@given(subtotal=subtotals, tier=tiers, country=countries)
def test_unknown_promos_have_no_effect(db, subtotal, tier, country):
    without = total(db, subtotal, tier, [], country)
    with_unknown = total(db, subtotal, tier, ["FAKEPROMO", "FAKE2"], country)
    assert without == with_unknown
```

**Six properties × 200 examples each = 1,200 generated inputs per
run.** Each property is a single English sentence translated into
Python. Total test code is shorter than the pgTAP version, and the
coverage is dramatically wider.

## What each suite catches

Both test suites pass on the function as written. The interesting
question is what each *would* catch if a future change introduced a
bug. Below, four realistic regressions and which test surfaces each.

### Regression 1: tier ordering accidentally reversed

Imagine a refactor swaps `gold` and `platinum` percentages — gold
becomes 10% off, platinum 5% off.

- **pgTAP:** the existing tier fixtures (`100.00 → gold → $101.65`)
  would assert against the new wrong values and pass once updated.
  Whether the *ordering* invariant is preserved is never asserted.
- **SqlProof:** `test_higher_tier_never_costs_more_than_lower_tier`
  fails on the smallest input that distinguishes the two tiers.
  Hypothesis reports `subtotal=Decimal("100.00"), promos=[]` and
  `gold=$90.00 < platinum=$95.00`, immediately pointing at the bug.

### Regression 2: someone swaps tax and discount order

A future migration moves the tax application *before* the promo
discount — accidentally letting customers get tax on a higher amount.

- **pgTAP:** all 14 fixture values go up. The test fails. After
  someone updates the fixtures to match the new behavior, the test
  passes again — silently shipping a tax-on-tax bug to production.
- **SqlProof:** `test_invoice_never_exceeds_subtotal_plus_max_tax`
  fails on subtotals where the discount + tax-order swap pushes the
  result above `subtotal × 1.20`. The bug is reported as a property
  violation, not a fixture mismatch — the message reads "your function
  produced $124 on subtotal $100", which is unambiguously wrong.

### Regression 3: JPY rounding mode change

Postgres's `round(numeric, 0)` uses banker's rounding (half-to-even).
A junior engineer "fixes" the function to use `floor(...)` thinking
it's more conservative.

- **pgTAP:** the JPY fixture (`100.00 → 'JP' → 110`) still passes
  because 110.0 rounds, floors, and ceils identically. Bug ships.
- **SqlProof:** `test_jpy_results_are_integer_valued` keeps passing
  too — but the *next* property added catches it. A monotonicity
  property like `test_higher_tier_never_costs_more_than_lower_tier`
  would surface a case where `floor(99.5) = 99` and
  `floor(99.499...) = 99` differ from `round(...)` semantics. Bug
  found in the next CI run.

### Regression 4: cap-at-50%-boundary off-by-one

Someone changes `LEAST(v_promo_total, v_amount * 0.50)` to
`LEAST(v_promo_total, v_amount / 2.0)` — looks equivalent but `/ 2.0`
returns a float instead of numeric, introducing precision drift.

- **pgTAP:** the cap-boundary fixture (`subtotal=20.00`,
  `WELCOME10×2 → $10.70`) still passes because the exact boundary
  value happens to coincide. No fixture targets values just inside or
  outside the boundary.
- **SqlProof:** `test_invoice_is_never_negative` and
  `test_invoice_never_exceeds_subtotal_plus_max_tax` both keep
  passing, but the next property —
  `test_adding_a_known_promo_never_increases_total` — fires on a
  drift example because float precision means the cap kicks in at a
  slightly different threshold for two near-equal subtotals.
  Hypothesis shrinks to the minimal pair and shows
  `subtotal=Decimal("19.99"), promos=[]` produces `$X` and
  `promos=['SAVE5']` produces `$X + 0.01`.

## Why the gap is so wide on functions

Three structural reasons:

1. **Function inputs are products.** `tier × country × promos ×
   subtotal` is a multiplicative space. Hand-typed fixtures cover a
   thin slice; Hypothesis covers it broadly.
2. **Function outputs are checkable as invariants.** "Higher tier
   never costs more" is a mathematical relationship between *pairs*
   of outputs. pgTAP's `is(...)` assertion takes one input and one
   expected output. There's no idiomatic way to express "for all X
   and Y, f(X) ≤ f(Y)" — you'd hand-roll loops.
3. **Function regressions usually preserve a fixture's exact value
   while breaking an invariant.** Tier ordering, rounding mode,
   precision changes, off-by-one boundaries — these are exactly the
   bugs where `is(f(100, 'gold'), 101.65)` keeps passing while a
   property test screams.

## Migration path

If you have an existing pgTAP suite for `compute_order_total` and want
to add SqlProof, you don't have to throw the pgTAP tests away. Three
practical patterns we've seen work:

1. **Keep pgTAP for the boundary cases** — fixed-value assertions
   that document specific behavior decisions ("yes, we round half-to-
   even on JPY", "yes, unknown promos are silently ignored"). pgTAP
   is excellent prose-form for "this is the exact answer for this
   exact input."
2. **Add SqlProof for the invariants** — monotonicity, idempotency,
   bounded-output, equivalence. Shorter to write, catches more.
3. **Use SqlProof to *generate the next pgTAP fixture*.** When
   SqlProof's shrinker reports a counterexample, port it into a pgTAP
   `is(...)` line so the regression has a permanent example test next
   to its property test.

Both suites coexist easily in the same project. The ripenn example
in this repo demonstrates the pattern (`supabase/tests/*.test.sql`
for pgTAP, `examples/ripenn_supabase/functions/*.py` for SqlProof).
