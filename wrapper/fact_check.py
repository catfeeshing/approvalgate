"""Financial-analyst source-of-truth check: every figure in a draft note is compared with fact_sheet.json.

Pipeline per sentence:
  normalize number words ("nineteen dollars" -> "19 dollars") -> find figures (with $, scale, %, ranges)
  -> tie each figure to the nearest metric keyword in its clause (or the sentence's running subject)
  -> resolve metric + period + segment + forward-looking context to a fact-sheet value
  -> compare with the persona's tolerances (0.5 pp on percentages, 0.5% on dollar amounts).

Result levels: BLOCK (contradicts the sheet / beyond guidance), HOLD_FOR_REVIEW (can't be verified),
OK (verified; kept as positive evidence for the reviewer).
"""
import re
from dataclasses import dataclass

BLOCK, HOLD, OK = "BLOCK", "HOLD_FOR_REVIEW", "OK"
PCT_TOL = 0.5          # percentage points (business rule 1)
USD_TOL = 0.005        # relative (business rule 1)


@dataclass
class Check:
    level: str
    message: str
    sentence: int = -1          # index of the sentence the check came from
    claim: str = ""             # the figure / phrase in the text, for UI highlighting


# ---------------------------------------------------------------- number words

UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen".split())}
TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
NUMWORD = r"(?:" + "|".join(list(UNITS) + list(TENS) + ["hundred", "thousand", "point", "a"]) + r")"
WORDSPAN = re.compile(
    rf"\b{NUMWORD}(?:[\s-]+(?:and[\s-]+)?{NUMWORD})*\b(?=\s*(?:percent|per cent|%|dollars?|cents?|million|billion|thousand|employees|people|diluted shares|shares))",
    re.I)


def words_to_number(span):
    toks = [t for t in re.split(r"[\s-]+", span.lower()) if t and t != "and"]
    if not toks or toks == ["a"]:
        return None
    total, current, decimals, in_decimal = 0, 0, "", False
    for t in toks:
        if t == "a":
            pass   # "a hundred" is handled by (current or 1) below; "a fourteen-day" must stay 14
        elif t == "point":
            in_decimal = True
        elif in_decimal:
            if t not in UNITS or UNITS[t] > 9:
                return None
            decimals += str(UNITS[t])
        elif t in UNITS:
            current += UNITS[t]
        elif t in TENS:
            current += TENS[t]
        elif t == "hundred":
            current = (current or 1) * 100
        elif t == "thousand":
            total += (current or 1) * 1000
            current = 0
    value = total + current
    return float(f"{value}.{decimals}") if decimals else float(value)


def normalize(text):
    text = text.replace("–", "-").replace("—", " - ").replace("’", "'")
    # "one dollar and forty-two cents" -> "1.42 dollars"
    def dollars_cents(m):
        d, c = words_to_number(m.group(1)), words_to_number(m.group(2))
        return f"{d + c / 100:.2f} dollars" if d is not None and c is not None else m.group(0)
    text = re.sub(rf"\b({NUMWORD}(?:[\s-]+{NUMWORD})*)\s+dollars?\s+and\s+({NUMWORD}(?:[\s-]+{NUMWORD})*)\s+cents\b",
                  dollars_cents, text, flags=re.I)

    def repl(m):
        n = words_to_number(m.group(0))
        return m.group(0) if n is None else (f"{n:g}")
    return WORDSPAN.sub(repl, text)


# ---------------------------------------------------------------- figures

NUM = re.compile(
    r"(?<![A-Za-z0-9.\-])(?P<cur>\$|USD\s?|US\$)?\s?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?![\d])(?!-[A-Za-z])"
    r"(?:\s?(?P<scale>million|billion|thousand|mn|bn|mm)\b|(?P<sscale>[mMbBkK])\b)?"
    r"(?P<tail>\s?(?:%|percent\b|per cent\b|percentage points?\b|pp\b|bps\b|basis points\b|dollars?\b|cents?\b))?",
    re.I)
SCALES = {"million": 1e6, "mn": 1e6, "mm": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9, "b": 1e9,
          "thousand": 1e3, "k": 1e3}


@dataclass
class Fig:
    start: int
    end: int
    raw: str
    value: float          # number as written (no scale)
    scale: float = None   # None = not stated
    currency: bool = False
    explicit_dollars: bool = False
    pct: bool = False
    delta_points: bool = False   # "percentage points" / bps: a change, not a level
    high: float = None           # set when this figure is a range low..high


def find_figures(s):
    figs = []
    for m in NUM.finditer(s):
        num = float(m.group("num").replace(",", ""))
        pre = s[max(0, m.start() - 4):m.start()]
        # skip fiscal years / quarters / plain calendar years
        if re.search(r"(FY|Q|H|fiscal\s?)$", pre, re.I) and not m.group("cur"):
            continue
        tail = (m.group("tail") or "").strip().lower()
        scale_word = (m.group("scale") or m.group("sscale") or "").lower()
        if (re.fullmatch(r"(19|20)\d\d", m.group("num")) and not m.group("cur") and not scale_word and not tail):
            continue
        f = Fig(m.start(), m.end(), m.group(0).strip(), num)
        f.currency = bool(m.group("cur")) or tail.startswith("dollar") or tail.startswith("cent")
        f.explicit_dollars = tail.startswith("dollar")
        if tail.startswith("cent"):
            f.value, f.scale = num / 100, 1.0
        if scale_word:
            f.scale = SCALES[scale_word]
        if tail in ("%", "percent", "per cent"):
            f.pct = True
        elif tail.startswith(("percentage point", "pp", "bps", "basis")):
            f.pct = f.delta_points = True
        figs.append(f)
    # merge ranges: "$440 million to $455 million", "$440-455 million", "between 440 and 455 million"
    merged, i = [], 0
    while i < len(figs):
        a = figs[i]
        if i + 1 < len(figs):
            b = figs[i + 1]
            gap = s[a.end:b.start]
            between = re.search(r"between\s+[^\s]*$", s[max(0, a.start - 12):a.start + 1], re.I)
            if re.fullmatch(r"\s*(-|to|through|and)\s*", gap, re.I) and (gap.strip() != "and" or between) and a.pct == b.pct:
                a.scale = a.scale or b.scale
                b.scale = b.scale or a.scale
                a.currency = a.currency or b.currency
                a.explicit_dollars = a.explicit_dollars or b.explicit_dollars
                a.high, a.end, a.raw = b.value, b.end, s[a.start:b.end].strip()
                merged.append(a)
                i += 2
                continue
        merged.append(a)
        i += 1
    return merged


# ---------------------------------------------------------------- metric keywords (order = precedence)

METRICS = [
    ("eps", r"\bEPS\b|earnings per share|per (?:diluted )?share"),
    ("unknown", r"\bEBITDA(?: margins?)?\b|(?:free |operating )?cash flow(?: margins?)?|(?:adjusted|pre-tax|pretax|contribution) margins?|\bcapex\b|capital expenditures?|dividends?|buybacks?|repurchases?|"
                r"market share|market cap(?:italization)?|valuation|price target|share price|stock|P/E|"
                r"R&D|research and development|customers?|gigawatt|\bGWh\b|\bMWh?\b|\bGW\b|installations?|"
                r"\bROE\b|\bROIC\b|return on|interest expense|tax rate|inventory|accounts receivable|"
                r"orders?|bookings|pipeline|utilization|efficiency|deliveries|units?|subscribers?"),
    ("shares", r"diluted shares|shares outstanding|share count|diluted share count|shares"),
    ("operating_margin", r"operating margins?|\bEBIT margins?"),
    ("gross_margin", r"gross margins?"),
    ("net_margin", r"net margins?|net profit margins?|profit margins?"),
    ("operating_income", r"operating (?:income|profit|earnings)|\bEBIT\b|income from operations"),
    ("gross_profit", r"gross profit"),
    ("net_income", r"net (?:income|profit|earnings)|bottom[- ]line|\bprofits?\b|\bearnings\b"),
    ("net_cash", r"net cash"),
    ("cash", r"\bcash(?: and (?:cash )?equivalents)?\b"),
    ("debt", r"\bdebt\b|borrowings|\bloans?\b"),
    ("backlog", r"backlog|order book"),
    ("employees", r"employees|headcount|workforce|\bstaff\b|people"),
    ("guidance", r"guid(?:ance|ed|ing|e)|outlook|forecasts?"),
    ("revenue", r"revenues?|\bsales\b|top[- ]line|turnover"),
    ("margin", r"\bmargins?\b"),
    ("growth", r"growth|\bgrew\b|\bgrow(?:s|ing)?\b|increase[ds]?|\brose\b|\brise\b|year[- ]over[- ]year|\byoy\b|\by/y\b"),
    ("share_of_total", r"of (?:the |total |group |company )*(?:total|revenue|sales)\b|share of|\bmix\b"),
]
METRIC_RE = [(name, re.compile(p, re.I)) for name, p in METRICS]
QUALIFIER_ONLY = {"growth", "share_of_total", "guidance"}   # modifiers rather than subjects

SEGMENT_RE = {
    "utility_storage": re.compile(r"utility[- ]storage|storage segment|storage business|\bstorage\b", re.I),
    "distributed_solar": re.compile(r"distributed[- ]solar|solar segment|solar business|\bsolar\b", re.I),
}
DECLINE = re.compile(r"\b(fell|fall(?:s|ing)?|declin\w*|decreas\w*|dropp?\w*|down\b|shr[ai]nk\w*|contract(?:ed|ion)|lower|slump\w*|plung\w*|weaker)\b", re.I)
GROWTH_WORD = re.compile(r"\b(up|grew|grow\w*|growth|increas\w*|rose|rise|rising|gain\w*|jump\w*|higher|climb\w*|expan\w*|year[- ]over[- ]year|yoy|y/y)\b", re.I)
MORE_THAN = re.compile(r"(more than|over|above|exceed\w*|at least|north of|surpass\w*|greater than|in excess of|top(?:ping)?)\s*$", re.I)
LESS_THAN = re.compile(r"(less than|under|below|at most|no more than|south of|short of)\s*$", re.I)
HEDGE = re.compile(r"(about|roughly|approximately|approx\.?|around|nearly|almost|close to|some|~)\s*$", re.I)
FORWARD = re.compile(r"\bFY\s?2026\b|\b(?:fiscal\s)?2026\b|next (?:fiscal )?year|\bexpect\w*|\bforecast\w*|\bproject(?:s|ed|ion|ions)?\b|"
                     r"\boutlook\b|\bguid\w*|\bwill\b|coming year|anticipat\w*|\bpredict\w*|\bon track to\b|\bset to\b", re.I)
Q4 = re.compile(r"\bQ4\b|fourth[- ]quarter|\bthe quarter\b|quarterly", re.I)
FY2024 = re.compile(r"\bFY\s?2024\b|\b(?:fiscal\s|in\s)2024\b|prior[- ]year|last year|a year (?:earlier|ago|before)|previous year", re.I)
FY2025 = re.compile(r"\bFY\s?2025\b|\b(?:fiscal\s|in\s)2025\b|this year|full[- ]year|year[- ]end", re.I)
PRIOR_HINT = re.compile(r"\b(from|versus|vs\.?|compared (?:with|to)|against)\s*$", re.I)

COMPANY = re.compile(r"\bbrightwater\b|\bthe company\b|\bthe firm\b|\bmanagement\b|\bits\b|\bthey\b|\bthe group\b", re.I)
EVENT = re.compile(r"\b(sign\w*|agreements?|contracts?|deals?|acquir\w*|acquisitions?|merg\w*|partner\w*|launch\w*|announc\w*|"
                   r"won|wins?|award\w*|expand\w*|entered|enter(?:s|ing)?|hir\w*|layoffs?|lawsuits?|litigation|approv\w*|CEO|CFO|"
                   r"record|largest|leading|leader|market leader|top-\w+|dividends?|buybacks?|upgrad\w*|downgrad\w*|rating|patents?|"
                   r"regulators?|investigat\w*|plans? to|intends? to|secured|selected|chosen|exclusive|breakthrough|"
                   r"profitable|outperform\w*|undervalued|overvalued|cheap|expensive|beat|miss(?:ed)?|recall\w*|restat\w*)\b", re.I)
PROMO = re.compile(r"\b(obviously|everyone knows|clearly|undeniabl\w*|no[- ]brainer|can'?t (?:lose|miss|go wrong)|guarante\w*|"
                   r"sure thing|must[- ]buy|must[- ]own|skyrocket\w*|to the moon|huge upside|massive upside|amazing|incredible|"
                   r"unbelievable|the best\b|best[- ]in[- ]class|revolutionary|game[- ]chang\w*|definitely|certainly|without (?:a )?doubt|"
                   r"risk[- ]free|once[- ]in[- ]a[- ]lifetime|world[- ]class|blowout|crush\w*|explod\w*|soar\w*)\b", re.I)


def split_sentences(text):
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z\"'(\$0-9])|\n+", text.strip())
    return [p.strip() for p in parts if p and p.strip()]


def clause_bounds(s, figs):
    """Clause boundaries at , ; : and coordinating words, except inside merged ranges."""
    protected = [(f.start, f.end) for f in figs if f.high is not None]
    cuts = [0]
    for m in re.finditer(r"[,;:]\s|\s(?:and|while|whereas|but|with)\s|\s-\s", s):
        if any(a <= m.start() < b for a, b in protected):
            continue
        cuts.append(m.start())
    cuts.append(len(s) + 1)
    return cuts


def keyword_hits(s):
    taken, hits = [], []
    for name, rx in METRIC_RE:
        for m in rx.finditer(s):
            if any(m.start() < b and a < m.end() for a, b in taken):
                continue
            taken.append((m.start(), m.end()))
            hits.append((m.start(), m.end(), name))
    return sorted(hits)


# ---------------------------------------------------------------- comparisons

def fmt_usd(v):
    if abs(v) >= 1e9:
        return f"${v / 1e9:,.4g} billion"
    if abs(v) >= 1e6:
        return f"${v / 1e6:,.1f} million"
    return f"${v:,.2f}"


def within(written, truth, kind, hedged=False):
    if kind == "pct":
        return abs(written - truth) <= PCT_TOL + (0.5 if hedged else 0)
    return abs(written - truth) <= abs(truth) * (USD_TOL + (0.01 if hedged else 0))


def compare(written, truth, kind, comparator, hedged):
    if comparator == "more":
        return truth >= written * (1 - (USD_TOL if kind != "pct" else 0)) - (PCT_TOL if kind == "pct" else 0)
    if comparator == "less":
        return truth <= written * (1 + (USD_TOL if kind != "pct" else 0)) + (PCT_TOL if kind == "pct" else 0)
    return within(written, truth, kind, hedged)


def show(v, kind):
    return f"{v:g}%" if kind == "pct" else (fmt_usd(v) if kind == "usd" else f"{v:,.0f}")


class FactSheet:
    def __init__(self, fs):
        self.fs = fs
        m = 1e6
        r, q, ni = fs["revenue"], fs["quarterly_revenue"], fs["net_income"]
        self.usd = {
            ("revenue", "FY2025"): r["FY2025"] * m, ("revenue", "FY2024"): r["FY2024"] * m,
            ("revenue", "Q4_FY2025"): q["Q4_FY2025"] * m, ("revenue", "Q4_FY2024"): q["Q4_FY2024"] * m,
            ("net_income", "FY2025"): ni["FY2025"] * m, ("net_income", "FY2024"): ni["FY2024"] * m,
            ("operating_income", "FY2025"): fs["operating_income"] * m,
            ("gross_profit", "FY2025"): r["FY2025"] * fs["gross_margin_pct"] / 100 * m,
            ("cash", "FY2025"): fs["balance_sheet"]["cash"] * m, ("debt", "FY2025"): fs["balance_sheet"]["debt"] * m,
            ("net_cash", "FY2025"): fs["balance_sheet"]["net_cash"] * m, ("backlog", "FY2025"): fs["backlog"] * m,
            ("eps", "FY2025"): fs["diluted_eps_usd"],
        }
        for seg, d in fs["segments"].items():
            self.usd[(seg, "FY2025")] = d["revenue"] * m
        self.pct = {
            ("revenue_growth", "FY2025"): r["growth_pct"], ("revenue_growth", "Q4_FY2025"): q["growth_pct"],
            ("net_income_growth", "FY2025"): ni["growth_pct"],
            ("gross_margin", "FY2025"): fs["gross_margin_pct"], ("operating_margin", "FY2025"): fs["operating_margin_pct"],
            ("net_margin", "FY2025"): round(ni["FY2025"] / r["FY2025"] * 100, 2),
        }
        for seg, d in fs["segments"].items():
            self.pct[(seg + "_share", "FY2025")] = d["share_pct"]
        g = fs["guidance"]
        self.guide_low, self.guide_high = g["FY2026_revenue_low"] * m, g["FY2026_revenue_high"] * m
        self.counts = {"employees": fs["employees"], "shares": fs["diluted_shares_millions"] * m}
        self.all_growth = [v for (k, _), v in self.pct.items() if k.endswith("growth")]
        self.all_margins = [self.pct[("gross_margin", "FY2025")], self.pct[("operating_margin", "FY2025")],
                            self.pct[("net_margin", "FY2025")]]


LABEL = {"revenue": "revenue", "net_income": "net income", "operating_income": "operating income",
         "gross_profit": "gross profit", "cash": "cash", "debt": "debt", "net_cash": "net cash",
         "backlog": "backlog", "eps": "diluted EPS", "utility_storage": "Utility Storage revenue",
         "distributed_solar": "Distributed Solar revenue", "revenue_growth": "revenue growth",
         "net_income_growth": "net income growth", "gross_margin": "gross margin",
         "operating_margin": "operating margin", "net_margin": "net margin (derived)",
         "utility_storage_share": "Utility Storage share of revenue", "distributed_solar_share": "Distributed Solar share of revenue"}


# ---------------------------------------------------------------- main entry

def check(text, fact_sheet, supported_claims=()):
    """Return (checks, coverage). coverage[i] is True when sentence i had at least one verified figure and nothing unverified."""
    sheet = FactSheet(fact_sheet)
    out, coverage = [], []
    subject = None   # running subject across sentences ("That is a 15% margin.")
    supported = [re.sub(r"\W+", " ", c.lower()).strip() for c in supported_claims]
    for si, raw_sentence in enumerate(split_sentences(text)):
        s = normalize(raw_sentence)
        figs = find_figures(s)
        hits = keyword_hits(s)
        cuts = clause_bounds(s, figs)
        forward = bool(FORWARD.search(s))
        q4 = bool(Q4.search(s))
        sentence_checks = []

        def add(level, msg, claim=""):
            sentence_checks.append(Check(level, msg, si, claim))

        def clause_of(pos):
            for a, b in zip(cuts, cuts[1:]):
                if a <= pos < b:
                    return a, b
            return 0, len(s)

        # walk figures in order, carrying a subject within the sentence
        for f in figs:
            ca, cb = clause_of(f.start)
            clause = s[ca:cb]
            in_clause = [(a, b, n) for a, b, n in hits if ca <= a < cb]
            subjects = [(a, b, n) for a, b, n in in_clause if n not in QUALIFIER_ONLY]
            quals = {n for _, _, n in in_clause if n in QUALIFIER_ONLY}
            if subjects:
                a, b, n = min(subjects, key=lambda h: min(abs(h[0] - f.end), abs(f.start - h[1])))
                metric = n
                if n not in ("margin",):
                    subject = n
            elif "growth" in quals and subject in (None, "revenue", "net_income"):
                metric = subject or "growth"
            elif "guidance" in quals:
                metric = "revenue"
            elif subject:
                metric = subject
            else:
                metric = "growth" if ("growth" in quals and f.pct) else None
            if metric is None and any(rx.search(s) for rx in SEGMENT_RE.values()):
                metric = "revenue"   # "Distributed Solar contributed $161 million": segment revenue
            before = s[max(ca, f.start - 30):f.start]
            comparator = "more" if MORE_THAN.search(before) else "less" if LESS_THAN.search(before) else None
            hedged = bool(HEDGE.search(before))
            fwd = forward or "guidance" in quals
            clause_period = ("Q4_" if q4 else "") + (
                "FY2024" if FY2024.search(clause) or PRIOR_HINT.search(before) else "FY2025")
            if q4 and FY2024.search(s) and not FY2025.search(clause) and not FY2024.search(clause):
                pass  # sentence-level FY mentions are ambiguous for Q4; keep clause-level reading
            # segment qualifier: clause first, then the last mention before the figure in the sentence
            seg = None
            for name, rx in SEGMENT_RE.items():
                if rx.search(clause):
                    seg = name
            if not seg:
                last = -1
                for name, rx in SEGMENT_RE.items():
                    for m in rx.finditer(s[:f.start]):
                        if m.start() > last:
                            last, seg = m.start(), name
            res = resolve(sheet, f, metric, quals, seg, clause_period, fwd, comparator, hedged, clause, subject)
            add(*res, claim=f.raw)

        # no-figure direction claims: "revenue declined in FY2025"
        if not figs:
            for a, b, n in hits:
                if n in ("revenue", "net_income") and DECLINE.search(s) and not forward:
                    add(BLOCK, f"Says {LABEL[n]} declined, but the fact sheet shows {LABEL[n]} grew "
                               f"({sheet.pct[(n + '_growth', 'FY2025')]:g}%).", s[a:b])
                    break
            # unverifiable statements about the company (rule 4)
            covered_by_ezio = any(c and (c in re.sub(r"\W+", " ", s.lower())) for c in supported)
            subject_hits = [n for _, _, n in hits if n not in QUALIFIER_ONLY]
            if not covered_by_ezio and not any(c.level == BLOCK for c in sentence_checks):
                if "unknown" in subject_hits:
                    add(HOLD, f"Claim about a metric that is not on the fact sheet: “{s}”", s)
                elif (COMPANY.search(s) or subject_hits or (forward and re.search(r"\bwe\b", s, re.I))) and EVENT.search(s):
                    add(HOLD, f"Claim about Brightwater that the fact sheet cannot verify: “{s}”",
                        EVENT.search(s).group(0))
                elif forward and (COMPANY.search(s) or subject_hits or re.search(r"\bwe\b", s, re.I)) and subject_hits:
                    add(HOLD, f"Forward-looking statement with no figure to check against guidance: “{s}”", s)

        out.extend(sentence_checks)
        levels = {c.level for c in sentence_checks}
        coverage.append(OK in levels and levels == {OK})
    return out, coverage


def resolve(sheet, f, metric, quals, seg, period, fwd, comparator, hedged, clause, subject):
    kind = "pct" if f.pct else "num"
    shown = f.raw
    if f.delta_points:
        return HOLD, f"“{shown}” is a change in percentage points; the fact sheet has no prior-period margins to verify it."

    if metric == "unknown":
        return HOLD, f"“{shown}” refers to a metric that is not on the fact sheet, so it cannot be verified."
    if metric is None:
        return HOLD, f"“{shown}” could not be tied to any fact-sheet metric; a human should confirm it."

    # ---- forward-looking revenue: must stay inside FY2026 guidance (rule 2)
    if fwd and metric in ("revenue", "growth", "guidance") and not seg:
        lo, hi = sheet.guide_low, sheet.guide_high
        if kind == "pct":
            base = sheet.usd[("revenue", "FY2025")]
            glo, ghi = (lo / base - 1) * 100, (hi / base - 1) * 100
            vals = [f.value] + ([f.high] if f.high is not None else [])
            bad = [v for v in vals if not (glo - PCT_TOL <= v <= ghi + PCT_TOL)]
            if comparator == "more" and f.value >= ghi:
                bad = [f.value]
            if bad:
                return BLOCK, (f"Forward growth of {shown} is outside FY2026 guidance, which implies "
                               f"{glo:.1f}%–{ghi:.1f}% growth (${lo/1e6:g}M–${hi/1e6:g}M).")
            return OK, f"Forward growth {shown} is consistent with FY2026 guidance ({glo:.1f}%–{ghi:.1f}%)."
        vals = [usd_value(f, f.value, "revenue")] + ([usd_value(f, f.high, "revenue")] if f.high is not None else [])
        glabel = f"FY2026 revenue guidance of ${lo/1e6:g}–${hi/1e6:g} million"
        if comparator == "more":
            if vals[0] >= hi * (1 - USD_TOL):
                return BLOCK, f"“{shown}” (more than {fmt_usd(vals[0])}) is beyond {glabel}."
            if vals[0] > lo * (1 + USD_TOL):
                return HOLD, f"“{shown}” covers only the top of {glabel}; a human should confirm the framing."
            return OK, f"“{shown}” is consistent with {glabel}."
        if comparator == "less":
            if vals[0] <= lo * (1 + USD_TOL):
                return BLOCK, f"“{shown}” (less than {fmt_usd(vals[0])}) is below {glabel}."
            return OK, f"“{shown}” is consistent with {glabel}."
        out = [v for v in vals if not (lo * (1 - USD_TOL) <= v <= hi * (1 + USD_TOL))]
        if out:
            return BLOCK, f"“{shown}” ({', '.join(fmt_usd(v) for v in out)}) is outside {glabel}."
        return OK, f"“{shown}” is within {glabel}."
    if fwd and metric not in ("revenue", "growth", "guidance"):
        return HOLD, f"Forward-looking figure “{shown}” for {LABEL.get(metric, metric)}: the only published guidance is FY2026 revenue."

    # ---- percentages
    if kind == "pct":
        key = None
        if metric in ("utility_storage", "distributed_solar") or (seg and (metric in ("revenue", "share_of_total") or "share_of_total" in quals)):
            key = ((seg or metric) + "_share", "FY2025")
        elif metric in ("gross_margin", "operating_margin", "net_margin"):
            key = (metric, "FY2025")
        elif metric == "margin":
            implied = {"operating_income": "operating_margin", "gross_profit": "gross_margin",
                       "net_income": "net_margin", "revenue": None}.get(subject)
            if implied:
                key = (implied, "FY2025")
            else:
                if any(within(f.value, t, "pct", hedged) for t in sheet.all_margins):
                    return OK, f"Margin {shown} matches a fact-sheet margin."
                return BLOCK, (f"Margin {shown} matches none of the reported margins (gross {sheet.all_margins[0]:g}%, "
                               f"operating {sheet.all_margins[1]:g}%).")
        elif metric in ("revenue",):
            key = ("revenue_growth", "Q4_FY2025" if period.startswith("Q4") else "FY2025")
        elif metric == "net_income":
            key = ("net_income_growth", "FY2025") if "margin" not in clause.lower() else ("net_margin", "FY2025")
        elif metric == "growth":
            if any(within(f.value, t, "pct", hedged) for t in sheet.all_growth) and not DECLINE.search(clause):
                return OK, f"Growth figure {shown} matches a reported growth rate."
            rates = ", ".join(f"{t:g}%" for t in sheet.all_growth)
            return BLOCK, f"Growth figure {shown} matches none of the reported growth rates ({rates})."
        elif metric in ("employees", "shares", "eps", "cash", "debt", "net_cash", "backlog", "operating_income", "gross_profit"):
            return HOLD, f"{shown} for {LABEL.get(metric, metric)}: the fact sheet has no percentage for this metric."
        if key is None or key not in sheet.pct:
            return HOLD, f"{shown} could not be matched to a fact-sheet percentage."
        if key[1] != "FY2025" and key not in sheet.pct:
            return HOLD, f"{shown}: the fact sheet has no {period} figure for {LABEL.get(key[0], key[0])}."
        truth = sheet.pct[key]
        if key[0].endswith("growth") and DECLINE.search(clause):
            return BLOCK, f"Says {LABEL[key[0]].replace(' growth', '')} fell {shown}; the fact sheet shows {truth:g}% growth."
        if compare(f.value, truth, "pct", comparator, hedged) and (f.high is None or compare(f.high, truth, "pct", None, hedged)):
            return OK, f"{LABEL[key[0]].capitalize()} {shown} matches the fact sheet ({truth:g}%)."
        return BLOCK, f"Text says {LABEL[key[0]]} is {shown}; the fact sheet says {truth:g}%."

    # ---- counts
    if metric == "employees":
        truth, v = sheet.counts["employees"], f.value * (f.scale or 1)
        if compare(v, truth, "num", comparator, hedged):
            return OK, f"Employees {shown} matches the fact sheet ({truth:,})."
        return BLOCK, f"Text says {shown} employees; the fact sheet says {truth:,}."
    if metric == "shares":
        truth, v = sheet.counts["shares"], f.value * (f.scale or 1e6)
        if compare(v, truth, "num", comparator, hedged):
            return OK, f"Diluted shares {shown} matches the fact sheet ({truth/1e6:g} million)."
        return BLOCK, f"Text says {shown} diluted shares; the fact sheet says {truth/1e6:g} million."

    # ---- dollar amounts
    if metric in ("revenue", "guidance") and seg:
        metric = seg
    if metric in ("margin", "gross_margin", "operating_margin", "net_margin"):
        if metric == "gross_margin":
            metric = "gross_profit"
        else:
            return BLOCK, f"“{shown}” is a dollar amount but {LABEL.get(metric, 'margin')} is a percentage ({metric.replace('_', ' ')} unit mismatch)."
    if metric in ("growth", "share_of_total"):
        return HOLD, f"“{shown}” could not be tied to a specific fact-sheet metric."
    if metric in ("revenue", "net_income"):
        key = (metric, period) if (metric, period) in sheet.usd else None
        if key is None:
            return HOLD, f"“{shown}”: the fact sheet has no {period.replace('_', ' ')} figure for {LABEL[metric]}."
    else:
        if period != "FY2025":
            annual = sheet.usd.get((metric, "FY2025"))
            v = usd_value(f, f.value, metric)
            if annual and period.startswith("Q4") and within(v, annual, "usd"):
                return BLOCK, f"“{shown}” is the annual {LABEL.get(metric, metric)} figure presented as a Q4 figure."
            return HOLD, f"“{shown}”: the fact sheet has no {period.replace('_', ' ')} figure for {LABEL.get(metric, metric)}."
        key = (metric, "FY2025")
    truth = sheet.usd[key]
    v = usd_value(f, f.value, metric)
    label = f"{LABEL.get(metric, metric)} ({key[1].replace('_', ' ')})"
    ok = compare(v, truth, "usd", comparator, hedged)
    if ok and f.high is not None:
        ok = compare(usd_value(f, f.high, metric), truth, "usd", None, hedged)
    if ok:
        rel = {"more": "is consistent with", "less": "is consistent with"}.get(comparator, "matches")
        return OK, f"{label.capitalize()} {shown} = {fmt_usd(v)} {rel} the fact sheet ({fmt_usd(truth)})."
    if within(v / (f.scale or (1 if metric == "eps" else 1e6)) * (1e6 if metric != "eps" else 1), truth, "usd") and f.scale:
        return BLOCK, f"“{shown}” has the wrong unit: {label} is {fmt_usd(truth)}, not {fmt_usd(v)}."
    return BLOCK, f"Text says {label} is {shown} ({fmt_usd(v)}); the fact sheet says {fmt_usd(truth)}."


def usd_value(f, number, metric):
    """Dollar value of a written number. Unscaled figures follow the fact sheet's convention (USD millions),
    except per-share figures and explicit 'N dollars'."""
    if f.scale:
        return number * f.scale
    if metric == "eps" or f.explicit_dollars:
        return number
    return number * 1e6


def promotional(text):
    """Independent tone check (rule 4): promotional or one-sided framing in the text itself."""
    return sorted({m.group(0).lower() for m in PROMO.finditer(text)})
