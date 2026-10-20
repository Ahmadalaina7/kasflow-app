"""Dutch tax rules for a sole trader (eenmanszaak), in cents.

Every figure comes from the Belastingdienst publications for that year
(tarieven box 1, ondernemersaftrek, heffingskortingen, Zvw, KIA). A year that is
not listed here falls back to the most recent known year, and the overview says
so, because guessing new figures would make the estimate silently wrong.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


def euros(value: int) -> int:
    return value * 100


def pct(value: str) -> Decimal:
    return Decimal(value) / 100


def apply_rate(cents: int, rate: Decimal) -> int:
    return int((Decimal(cents) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class TaxYear:
    year: int
    # (upper bound in cents or None for the top bracket, rate)
    brackets: tuple[tuple[int | None, Decimal], ...]
    # Entrepreneur deductions only yield tax relief at this rate.
    deduction_rate: Decimal
    self_employed_deduction: int
    starter_deduction: int
    mkb_exemption: Decimal
    general_credit_max: int
    general_credit_start: int
    general_credit_rate: Decimal
    # (upper bound, base amount, rate, lower bound) per segment of the arbeidskorting
    labour_credit: tuple[tuple[int, int, Decimal, int], ...]
    zvw_rate: Decimal
    zvw_cap: int
    km_rate_cents: int
    kia_floor: int
    kia_full_until: int
    kia_flat_until: int
    kia_flat_amount: int
    kia_end: int
    hours_criterion: int = 1225
    kia_item_minimum: int = euros(450)
    kia_rate: Decimal = pct("28")
    kia_phaseout_rate: Decimal = pct("7.56")


TAX_YEARS: dict[int, TaxYear] = {
    2025: TaxYear(
        year=2025,
        brackets=(
            (euros(38_441), pct("35.82")),
            (euros(76_817), pct("37.48")),
            (None, pct("49.50")),
        ),
        deduction_rate=pct("37.48"),
        self_employed_deduction=euros(2_470),
        starter_deduction=euros(2_123),
        mkb_exemption=pct("12.70"),
        general_credit_max=euros(3_068),
        general_credit_start=euros(28_406),
        general_credit_rate=pct("6.337"),
        labour_credit=(
            (euros(12_169), 0, pct("8.053"), 0),
            (euros(26_288), euros(980), pct("30.030"), euros(12_169)),
            (euros(43_071), euros(5_220), pct("2.258"), euros(26_288)),
            (euros(129_078), euros(5_599), pct("-6.510"), euros(43_071)),
        ),
        zvw_rate=pct("5.26"),
        zvw_cap=euros(75_864),
        km_rate_cents=23,
        kia_floor=euros(2_900),
        kia_full_until=euros(70_602),
        kia_flat_until=euros(130_744),
        kia_flat_amount=euros(19_769),
        kia_end=euros(392_230),
    ),
    2026: TaxYear(
        year=2026,
        brackets=(
            (euros(38_883), pct("35.75")),
            (euros(78_426), pct("37.56")),
            (None, pct("49.50")),
        ),
        deduction_rate=pct("37.56"),
        self_employed_deduction=euros(1_200),
        starter_deduction=euros(2_123),
        mkb_exemption=pct("12.70"),
        general_credit_max=euros(3_115),
        general_credit_start=euros(29_736),
        general_credit_rate=pct("6.398"),
        labour_credit=(
            (euros(11_965), 0, pct("8.324"), 0),
            (euros(25_845), euros(996), pct("31.009"), euros(11_965)),
            (euros(45_592), euros(5_300), pct("1.950"), euros(25_845)),
            (euros(132_920), euros(5_685), pct("-6.510"), euros(45_592)),
        ),
        zvw_rate=pct("4.85"),
        zvw_cap=euros(79_409),
        km_rate_cents=23,
        kia_floor=euros(2_900),
        kia_full_until=euros(71_683),
        kia_flat_until=euros(132_746),
        kia_flat_amount=euros(20_072),
        kia_end=euros(398_236),
    ),
}


def tax_year(year: int) -> tuple[TaxYear, bool]:
    """Return the rules for ``year`` and whether they are the exact ones."""
    if year in TAX_YEARS:
        return TAX_YEARS[year], True
    known = max(TAX_YEARS) if year > max(TAX_YEARS) else min(TAX_YEARS)
    return TAX_YEARS[known], False


# ---------------------------------------------------------------------------
# Expense categories
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpenseCategory:
    key: str
    label: str
    # Whether the VAT on the receipt may be deducted as voorbelasting.
    vat_deductible: bool = True
    # Share of the cost that is deductible for income tax.
    ib_share: Decimal = Decimal("1")
    asset: bool = False
    hint: str = ""


EXPENSE_CATEGORIES: tuple[ExpenseCategory, ...] = (
    ExpenseCategory("inkoop", "Inkoop en materiaal"),
    ExpenseCategory("kantoor", "Kantoorbenodigdheden"),
    ExpenseCategory("software", "Software en abonnementen"),
    ExpenseCategory("telefoon", "Telefoon en internet"),
    ExpenseCategory("marketing", "Marketing en advertenties"),
    ExpenseCategory("reis", "Reiskosten (ov, taxi, parkeren)"),
    ExpenseCategory("auto", "Kosten zakelijke auto"),
    ExpenseCategory("huisvesting", "Huur en huisvesting"),
    ExpenseCategory("opleiding", "Opleiding en vakliteratuur"),
    ExpenseCategory("advies", "Advies en administratie"),
    ExpenseCategory("verzekering", "Verzekeringen", hint="Op verzekeringen zit geen btw."),
    ExpenseCategory("bank", "Bankkosten", hint="Op bankkosten zit geen btw."),
    ExpenseCategory(
        "horeca",
        "Eten en drinken (horeca)",
        vat_deductible=False,
        ib_share=Decimal("0.80"),
        hint="Btw op eten en drinken in de horeca is niet aftrekbaar; de kosten tellen voor 80% mee.",
    ),
    ExpenseCategory(
        "representatie",
        "Representatie en relatiegeschenken",
        ib_share=Decimal("0.80"),
        hint="Telt voor 80% mee voor de inkomstenbelasting.",
    ),
    ExpenseCategory("overig", "Overige zakelijke kosten"),
    ExpenseCategory(
        "investering",
        "Investering (bedrijfsmiddel vanaf € 450)",
        asset=True,
        hint="Wordt afgeschreven over minimaal 5 jaar en telt mee voor de investeringsaftrek.",
    ),
)

CATEGORY_BY_KEY = {category.key: category for category in EXPENSE_CATEGORIES}

# Free-text categories stored before categories had tax rules.
LEGACY_CATEGORIES = {
    "Kantoor": "kantoor",
    "Reiskosten": "reis",
    "Representatie": "representatie",
    "Software": "software",
    "Materiaal": "inkoop",
    "Overig": "overig",
}


def expense_category(value: str) -> ExpenseCategory:
    key = LEGACY_CATEGORIES.get(value, value)
    return CATEGORY_BY_KEY.get(key, CATEGORY_BY_KEY["overig"])


# How VAT on a purchase is handled in the VAT return.
PURCHASE_VAT_ORIGINS = {
    "nl": "Nederlandse btw op de bon",
    "none": "Geen btw (vrijgesteld of particulier)",
    "reverse_nl": "Btw naar mij verlegd (Nederland)",
    "eu": "Uit een ander EU-land, btw verlegd",
    "non_eu": "Van buiten de EU, btw verlegd",
}

# Purchases where the buyer declares the VAT itself, with their rubric.
REVERSE_CHARGE_RUBRICS = {"reverse_nl": "2a", "eu": "4b", "non_eu": "4a"}


# ---------------------------------------------------------------------------
# Income tax
# ---------------------------------------------------------------------------


def kia_amount(rules: TaxYear, invested: int) -> int:
    if invested <= rules.kia_floor or invested > rules.kia_end:
        return 0
    if invested <= rules.kia_full_until:
        return apply_rate(invested, rules.kia_rate)
    if invested <= rules.kia_flat_until:
        return rules.kia_flat_amount
    reduction = apply_rate(invested - rules.kia_flat_until, rules.kia_phaseout_rate)
    return max(0, rules.kia_flat_amount - reduction)


def labour_credit(rules: TaxYear, income: int) -> int:
    if income <= 0:
        return 0
    for upper, base, rate, lower in rules.labour_credit:
        if income <= upper:
            return max(0, base + apply_rate(income - lower, rate))
    return 0


def general_credit(rules: TaxYear, income: int) -> int:
    if income <= rules.general_credit_start:
        return rules.general_credit_max
    reduction = apply_rate(income - rules.general_credit_start, rules.general_credit_rate)
    return max(0, rules.general_credit_max - reduction)


def box1_tax(rules: TaxYear, income: int) -> int:
    if income <= 0:
        return 0
    tax = 0
    lower = 0
    for upper, rate in rules.brackets:
        top = income if upper is None else min(income, upper)
        if top > lower:
            tax += apply_rate(top - lower, rate)
        if upper is None or income <= upper:
            break
        lower = upper
    return tax


@dataclass(frozen=True)
class IncomeTaxEstimate:
    profit: int
    hours_criterion_met: bool
    self_employed_deduction: int
    starter_deduction: int
    mkb_exemption: int
    taxable_profit: int
    box1_tax: int
    deduction_correction: int
    general_credit: int
    labour_credit: int
    income_tax: int
    zvw: int
    total: int


def estimate_income_tax(
    rules: TaxYear, profit: int, *, hours_criterion_met: bool, starter: bool
) -> IncomeTaxEstimate:
    """Income tax and Zvw for someone whose only income is this business.

    ``profit`` is the fiscal profit after depreciation and investment
    deduction, before the entrepreneur deductions.
    """
    self_employed = 0
    starter_amount = 0
    if hours_criterion_met:
        self_employed = rules.self_employed_deduction
        if starter:
            starter_amount = rules.starter_deduction
        elif profit < self_employed:
            # Without the starter deduction it cannot exceed the profit.
            self_employed = max(0, profit)
    after_deductions = profit - self_employed - starter_amount
    mkb = apply_rate(after_deductions, rules.mkb_exemption)
    taxable = after_deductions - mkb

    tax = box1_tax(rules, taxable)
    # Deductions only give relief at the deduction rate, also when the income
    # lands in the top bracket.
    second_upper = rules.brackets[-2][0] or 0
    top_rate = rules.brackets[-1][1]
    deductions = self_employed + starter_amount + max(0, mkb)
    correction = apply_rate(
        min(deductions, max(0, taxable - second_upper)), top_rate - rules.deduction_rate
    )
    tax += correction

    labour = labour_credit(rules, max(0, profit))
    general = general_credit(rules, max(0, taxable))
    income_tax = max(0, tax - labour - general)
    zvw = apply_rate(min(max(0, taxable), rules.zvw_cap), rules.zvw_rate)
    return IncomeTaxEstimate(
        profit=profit,
        hours_criterion_met=hours_criterion_met,
        self_employed_deduction=self_employed,
        starter_deduction=starter_amount,
        mkb_exemption=mkb,
        taxable_profit=taxable,
        box1_tax=tax - correction,
        deduction_correction=correction,
        general_credit=min(general, max(0, tax - labour)),
        labour_credit=min(labour, tax),
        income_tax=income_tax,
        zvw=zvw,
        total=income_tax + zvw,
    )


def month_index(day) -> int:
    return day.year * 12 + day.month - 1


def depreciation_between(
    cost: int, residual: int, life_years: int, purchased, first_month: int, last_month: int
) -> int:
    """Straight-line depreciation from the month of purchase, for the months
    ``first_month`` up to and including ``last_month`` (see ``month_index``)."""
    base = max(0, cost - residual)
    total_months = life_years * 12
    start = month_index(purchased)

    def done_before(month: int) -> int:
        return base * min(total_months, max(0, month - start)) // total_months

    return done_before(last_month + 1) - done_before(first_month)
