"""Customer-support source-of-truth check: a draft email is compared with price_sheet.csv and policies.json.

Per sentence: normalize number words -> find prices ($), percentages, and durations (hours/days/months/years)
-> tie each to the nearest topic keyword in its clause (price, discount, refund, trial, response time, tenure)
-> resolve the plan (named in the text, otherwise the customer's plan from context) -> compare with the data.
Also: discount eligibility from context (plan, tenure), no stacking, Enterprise is custom-priced,
and promises agents can't make (features, dates, exceptions) are held for a manager.

Result levels: BLOCK (contradicts the data), HOLD_FOR_REVIEW (needs a manager), OK (verified evidence).
"""
import re

from fact_check import BLOCK, HOLD, OK, Check, NUMWORD, clause_bounds, find_figures, normalize, split_sentences, words_to_number

PLAN_RE = re.compile(r"\b(Starter|Pro|Business|Enterprise)\b")
SELF_SERVE = ("Starter", "Pro", "Business")

TOPICS = [
    ("stacking", r"\b(combin\w*|stack\w*|on top of|together|in addition to|plus your|as well as|both|at the same time|along with)\b"),
    ("loyalty", r"\bloyal\w*|long[- ]time customer|long[- ]standing customer"),
    ("annual_discount", r"annual(?:ly)? billing discount|annual(?:ly)?[- ]billing|billed (?:annually|yearly)|annual (?:plan )?discount|pay(?:ing)? (?:annually|yearly|for a year)|yearly billing"),
    ("refund", r"\brefund\w*|money[- ]back"),
    ("trial", r"\btrial\b"),
    ("response", r"\b(first[- ]response|respon[ds]\w*|reply|replies|replied|get back to you|hear (?:back|from)|response time|SLA|reach out to you|contact you|turnaround|answer)\b"),
    ("tenure", r"\b(customer (?:of|for)|with us (?:for)?|member (?:for|of)|subscriber (?:for|of)|been (?:a customer|with us)|tenure)\b"),
    ("discount", r"\bdiscount\w*|\boff\b|\bsav(?:e|ing|ings)\b|\bcoupon\b|\bpromo\w*"),
    ("price", r"\bprice[ds]?\b|\bcost[s]?\b|\bpric\w+|\bruns\b|\bis\b|\$|\bper month\b|/mo\b|\bmonthly\b"),
]
TOPIC_RE = [(n, re.compile(p, re.I)) for n, p in TOPICS]

PER_MONTH = re.compile(r"per month|/\s?mo(?:nth)?\b|a month|each month|monthly|per-month|every month", re.I)
PER_YEAR = re.compile(r"per year|/\s?y(?:ea)?r\b|a year|annually|yearly|per annum|billed annually|annual", re.I)
ANNUAL_PLAN = re.compile(r"annual(?:ly)?|yearly|year[- ]long|12[- ]month plan|per year|a year plan", re.I)
MONTHLY_PLAN = re.compile(r"\bmonthly\b|month[- ]to[- ]month", re.I)

DURATION = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*-?\s*(hours?|hrs?|business days?|days?|weeks?|months?|years?)\b", re.I)
FOREVER = re.compile(r"\b(any ?time|forever|no time limit|whenever you (?:like|want)|at any point|for life|lifetime)\b", re.I)
A_YEAR = re.compile(r"\b(?:the |a |one |first |full )year\b", re.I)
WORD_DURATION = re.compile(rf"\b({NUMWORD}(?:[\s-]+(?:and[\s-]+)?{NUMWORD})*)(?=[\s-]+(?:hours?|days?|weeks?|months?|years?)\b)", re.I)

MORE = re.compile(r"(more than|over|at least|longer than|above)\s*$", re.I)
CARD_REQUIRED = re.compile(r"(?:credit )?card (?:is )?required|requires? a (?:credit )?card|need (?:a|your) (?:credit )?card", re.I)


def card_required_claim(s):
    m = CARD_REQUIRED.search(s)
    if not m or re.search(r"\b(no|without|not|never|n't)\b[\w\s]{0,12}$", s[:m.start()], re.I) or re.search(r"\bnot\b|n't", m.group(0)):
        return None
    return m

PROMISE_VERB = re.compile(r"\b(I'?ll|I will|we'?ll|we will|I promise|we promise|I guarantee|we guarantee|make sure|going to|will|can get you)\b", re.I)
PROMISE_OBJ = re.compile(
    r"\b(features?|ships?|shipping|shipped|release[ds]?|roadmap|engineering|engineers|developers?|dev team|product team|build(?:s|ing)?|"
    r"integrations?|add support|launch\w*|fix(?:ed)? (?:by|before|within)|by (?:next|this|the end of (?:the )?) ?(?:week|month|quarter|year)|"
    r"next (?:week|month|quarter|release|sprint|update)|exceptions?|waive\w*|free (?:month|upgrade)|extend\w* (?:your )?trial|"
    r"on the house|credit (?:to )?your account|deliver\w*|arriv\w*|custom (?:price|pricing|discount|deal)|special (?:price|pricing|deal|discount))\b",
    re.I)
# Rule 3, generalized: strong promises, deliverables by a date, and "you will have X by <date>".
STRONG_PROMISE = re.compile(r"\b(I|we) (?:can )?(?:promise|guarantee)\b|\b(?:I|we)(?:'ll| will) make sure\b|you have my word|rest assured|"
                            r"\bi swear\b|count on (?:it|me|us)", re.I)
FUTURE = re.compile(r"\b(will|'ll|going to|gonna|shall)\b", re.I)
DATE = re.compile(r"\b(?:by|before|on|until|no later than|in time for)\s+(?:the\s+)?(?:end of (?:the )?(?:week|month|quarter|year)|"
                  r"today|tonight|tomorrow|this (?:week|weekend|month)|next (?:week|month|quarter|year|\w+day)|"
                  r"(?:mon|tues|wednes|thurs|fri|satur|sun)day|weekend|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\.?\s*\d{0,2}|"
                  r"\d{1,2}(?:st|nd|rd|th)|\d{1,2}/\d{1,2}|q[1-4])\b|\bnext (?:week|month|quarter|release|sprint)\b|\bwithin (?:a|\d+) (?:days?|weeks?|months?)\b", re.I)
DELIVERABLE = re.compile(r"\b(ready|available|live|fixed|released|shipped|delivered|launched|built|done|finished|completed|deployed|"
                         r"rolled out|working|resolved|in place|set up|up and running|added|implemented)\b", re.I)
YOU_WILL_HAVE = re.compile(r"\byou(?:'ll| will| are going to) (?:have|get|receive|see)\b", re.I)
# the agent's own follow-ups are not product promises ("I'll send you an update by end of day")
FOLLOW_UP = re.compile(r"\b(update|reply|response|answer|email|call|follow[- ]?up|get back|look into|check (?:in|on)|escalat\w*|"
                       r"let you know|reach out|keep you posted|send (?:you )?(?:an? |the |your )?(?:update|email|note|summary|invoice|receipt|link|details|copy))\b", re.I)
PRODUCT = re.compile(r"\b(feature|dashboard|integration|export|import|api|report(?:ing)?|tool|module|app|version|fix|patch|bug ?fix|"
                     r"functionality|capability|support for|dark mode|sso|plugin|add-?on|workflow|template|analytics|account|migration|server|data)\b", re.I)


def promise_claim(s):
    """Return the phrase that makes this sentence a promise an agent can't make, or None."""
    m = PROMISE_VERB.search(s) and PROMISE_OBJ.search(s)
    if m:
        return m.group(0)
    product, follow = PRODUCT.search(s), FOLLOW_UP.search(s)
    only_follow_up = follow and not product
    strong = STRONG_PROMISE.search(s)
    if strong and not only_follow_up:
        return strong.group(0)
    date = DATE.search(s)
    if date and FUTURE.search(s) and DELIVERABLE.search(s) and not only_follow_up:
        return date.group(0)
    if date and YOU_WILL_HAVE.search(s) and not only_follow_up:
        return date.group(0)
    return None


HYPE = re.compile(r"\b(amazing|incredible|unbelievable|guarantee[ds]?|best (?:deal|plan|price|value|choice) ever|unbeatable|"
                  r"you(?:'ll| will) love|life[- ]changing|revolutionary|act now|don'?t miss|once[- ]in[- ]a[- ]lifetime|"
                  r"risk[- ]free|100% satisf\w*|no[- ]brainer|insane(?:ly)?|mind[- ]blowing|best[- ]in[- ]class)\b", re.I)


def promotional(text):
    words = {m.group(0).lower() for m in HYPE.finditer(text)}
    if re.search(r"!{2,}", text):
        words.add("repeated exclamation marks")
    return sorted(words)


def normalize_durations(s):
    def repl(m):
        n = words_to_number(m.group(1))
        return m.group(0) if n is None else f"{n:g}"
    return WORD_DURATION.sub(repl, s)


def to_days(n, unit):
    u = unit.lower()
    if u.startswith(("hour", "hr")):
        return n / 24
    if u.startswith("business day"):
        return n * 7 / 5
    if u.startswith("day"):
        return n
    if u.startswith("week"):
        return n * 7
    if u.startswith("month"):
        return n * 30
    return n * 365


class Data:
    def __init__(self, policies, price_rows):
        self.p = policies
        self.plans = {r["plan"]: r for r in price_rows}
        d = policies["discounts"]
        self.loyalty_pct, self.loyalty_plans, self.loyalty_tenure = d["loyalty"]["percent"], d["loyalty"]["applies_to"], d["loyalty"]["min_tenure_months"]
        self.annual_pct, self.annual_plans = d["annual_billing"]["percent"], d["annual_billing"]["applies_to"]
        self.stacking = d["stacking_allowed"]
        self.refund_monthly, self.refund_annual = policies["refunds"]["monthly_plans_days"], policies["refunds"]["annual_plans_days"]
        self.trial_days, self.trial_plans = policies["free_trial"]["days"], policies["free_trial"]["plans"]
        self.card_required = policies["free_trial"]["card_required"]

    def monthly(self, plan):
        return float(self.plans[plan]["monthly_price_usd"])

    def annual_pm(self, plan):
        return float(self.plans[plan]["annual_price_per_month_usd"])

    def response_hours(self, plan):
        return float(self.plans[plan]["first_response_hours"])

    def trial(self, plan):
        return float(self.plans[plan]["free_trial_days"])


def price_ok(written, truth, hedged=False):
    return abs(written - truth) <= max(0.01, truth * (0.02 if hedged else 0.002))


def check(text, context, policies, price_rows, supported_claims=()):
    data = Data(policies, price_rows)
    ctx_plan = context.get("plan") if isinstance(context.get("plan"), str) else None
    ctx_plan = ctx_plan if ctx_plan in data.plans or ctx_plan == "Enterprise" else None
    tenure = context.get("customer_tenure_months")
    tenure = tenure if isinstance(tenure, (int, float)) and not isinstance(tenure, bool) else None
    out, coverage = [], []
    last_plan = None

    for si, raw in enumerate(split_sentences(text)):
        s = normalize_durations(normalize(raw))
        checks = []

        def add(level, msg, claim=""):
            checks.append(Check(level, msg, si, claim))

        figs = [f for f in find_figures(s) if f.currency or f.pct]
        durs = [(m.start(), m.end(), float(m.group(1)), m.group(2)) for m in DURATION.finditer(s)]
        cuts = clause_bounds(s, figs)
        topics = sorted((m.start(), m.end(), n) for n, rx in TOPIC_RE for m in rx.finditer(s))
        plans_here = [(m.start(), m.group(1)) for m in PLAN_RE.finditer(s)]
        addressed = bool(re.search(r"\b(you|your)\b", s, re.I))
        every_plan = bool(re.search(r"\b(every|all|each|any)\b[^.]{0,20}\bplans?\b", s, re.I))

        def clause_of(pos):
            for a, b in zip(cuts, cuts[1:]):
                if a <= pos < b:
                    return a, b
            return 0, len(s)

        def plan_for(pos):
            a, b = clause_of(pos)
            inside = [p for x, p in plans_here if a <= x < b]
            if inside:
                return inside[0]
            before = [p for x, p in plans_here if x < pos]
            if before:
                return before[-1]
            if plans_here:
                return plans_here[0][1]
            return last_plan or ctx_plan

        def topic_for(pos, end, skip=()):
            a, b = clause_of(pos)
            cands = [(x, y, n) for x, y, n in topics if a <= x < b and n not in skip]
            if not cands:
                cands = [(x, y, n) for x, y, n in topics if n not in skip]
            if not cands:
                return None
            # specific topics beat the generic "discount"/"price" words
            rank = {n: i for i, (n, _) in enumerate(TOPICS)}
            cands.sort(key=lambda h: (min(abs(h[0] - end), abs(pos - h[1])) > 60, rank[h[2]], min(abs(h[0] - end), abs(pos - h[1]))))
            return cands[0][2]

        has = {n for _, _, n in topics}

        # ---- stacking (rule 2): combining loyalty and annual discounts
        if not data.stacking:
            both = "loyalty" in has and ("annual_discount" in has or re.search(r"\bannual\b", s, re.I))
            stacked_pct = [f for f in figs if f.pct and round(f.value) in (data.loyalty_pct + data.annual_pct,
                                                                           round(100 - (100 - data.loyalty_pct) * (100 - data.annual_pct) / 100))]
            if (both and "stacking" in has) or (stacked_pct and ("loyalty" in has or "annual_discount" in has or "stacking" in has)):
                add(BLOCK, f"Discounts cannot be stacked: the loyalty ({data.loyalty_pct}%) and annual billing ({data.annual_pct}%) "
                           f"discounts can't be combined.", (stacked_pct[0].raw if stacked_pct else "combine"))

        # ---- percentages
        for f in figs:
            if not f.pct:
                continue
            if any(c.level == BLOCK and c.claim == f.raw for c in checks):
                continue
            topic = topic_for(f.start, f.end, skip=("price", "stacking"))
            plan = plan_for(f.start)
            if topic == "loyalty":
                if abs(f.value - data.loyalty_pct) > 0.01:
                    add(BLOCK, f"Loyalty discount is {data.loyalty_pct}%, the email says {f.raw}.", f.raw)
                else:
                    add(OK, f"Loyalty discount {f.raw} matches policy ({data.loyalty_pct}%).", f.raw)
            elif topic == "annual_discount":
                if abs(f.value - data.annual_pct) > 0.01:
                    add(BLOCK, f"Annual billing discount is {data.annual_pct}%, the email says {f.raw}.", f.raw)
                else:
                    add(OK, f"Annual billing discount {f.raw} matches policy ({data.annual_pct}%).", f.raw)
            elif topic in ("discount", None) and re.search(r"\b(off|discount\w*|sav\w+)\b", s, re.I):
                if any(abs(f.value - p) <= 0.01 for p in (data.loyalty_pct, data.annual_pct)):
                    add(OK, f"Discount {f.raw} matches a policy discount (loyalty {data.loyalty_pct}%, annual {data.annual_pct}%).", f.raw)
                else:
                    add(BLOCK, f"A {f.raw} discount is not in policy (only loyalty {data.loyalty_pct}% or annual billing "
                               f"{data.annual_pct}%), and agents can't offer discounts beyond policy.", f.raw)
            else:
                add(HOLD, f"“{f.raw}” can't be checked against the price sheet or policies.", f.raw)

        # ---- loyalty eligibility (rule 2), using the customer's context
        if "loyalty" in has and not any(c.level == BLOCK and "stack" in c.message for c in checks):
            m = next((x for x in topics if x[2] == "loyalty"), None)
            plan = plan_for(m[0]) if m else ctx_plan
            elig_plan = plan
            negated = re.search(r"\b(not|n't|isn't|aren't|doesn't|don't|no longer|ineligible)\b", s, re.I)
            if not negated:
                if elig_plan and elig_plan not in data.loyalty_plans:
                    add(BLOCK, f"The loyalty discount applies only to {' and '.join(data.loyalty_plans)}, not {elig_plan}.", elig_plan)
                elif addressed and tenure is not None and tenure < data.loyalty_tenure:
                    add(BLOCK, f"The customer has {tenure:g} months of tenure; the loyalty discount needs {data.loyalty_tenure}.", "loyalty")
                elif addressed and ctx_plan and ctx_plan not in data.loyalty_plans and not plans_here:
                    add(BLOCK, f"The customer is on {ctx_plan}; the loyalty discount applies only to {' and '.join(data.loyalty_plans)}.", "loyalty")
                elif addressed and tenure is not None:
                    add(OK, f"Customer is eligible for the loyalty discount ({elig_plan or ctx_plan}, {tenure:g} months ≥ {data.loyalty_tenure}).", "loyalty")

        # ---- annual discount eligibility
        if "annual_discount" in has:
            for x, p in plans_here:
                if p not in data.annual_plans and not re.search(r"\bnot\b|n't", s, re.I):
                    add(BLOCK, f"The annual billing discount applies to {', '.join(data.annual_plans)}, not {p}.", p)

        # ---- prices
        for f in figs:
            if f.pct:
                continue
            plan = plan_for(f.start)
            a, b = clause_of(f.start)
            clause = s[a:b]
            near = s[f.end:f.end + 25]
            hedged = bool(re.search(r"(about|roughly|around|approximately|~)\s*$", s[max(0, f.start - 15):f.start], re.I))
            value = f.value * (f.scale or 1)
            if re.search(r"\b(off|discount|save|saving|savings)\b", s[f.end:f.end + 20] + s[max(0, f.start - 20):f.start], re.I) and not PER_MONTH.search(near):
                add(HOLD, f"Dollar discount “{f.raw}” isn't defined in policy; a manager should confirm it.", f.raw)
                continue
            if plan == "Enterprise":
                add(BLOCK, f"Enterprise pricing is custom (contact sales); the email quotes {f.raw}.", f.raw)
                continue
            if plan is None:
                matches = [p for p in data.plans if price_ok(value, data.monthly(p), hedged) or price_ok(value, data.annual_pm(p), hedged)]
                if matches:
                    add(OK, f"Price {f.raw} matches the {matches[0]} plan.", f.raw)
                else:
                    add(HOLD, f"Price “{f.raw}” isn't tied to a plan and matches no price on the sheet.", f.raw)
                continue
            pm, py = PER_MONTH.search(near) or PER_MONTH.search(clause), PER_YEAR.search(clause)
            monthly, annual_pm = data.monthly(plan), data.annual_pm(plan)
            if py and not pm:
                ok = price_ok(value, annual_pm * 12, hedged) or price_ok(value, annual_pm, hedged)
                truth = f"${annual_pm * 12:,.2f} per year (${annual_pm:.2f}/mo billed annually)"
            elif py and pm:
                ok = price_ok(value, annual_pm, hedged)
                truth = f"${annual_pm:.2f} per month billed annually"
            else:
                ok = price_ok(value, monthly, hedged) or (not pm and price_ok(value, annual_pm, hedged))
                truth = f"${monthly:g} per month"
            if ok:
                add(OK, f"{plan} price {f.raw} matches the price sheet ({truth}).", f.raw)
            else:
                add(BLOCK, f"{plan} is {truth}; the email says {f.raw}.", f.raw)

        # ---- durations: refunds, trials, response times, tenure
        for start, end, n, unit in durs:
            topic = topic_for(start, end, skip=("price", "discount", "stacking", "loyalty", "annual_discount"))
            if topic is None and unit.lower().startswith(("hour", "hr")):
                topic = "response"   # the only hour-based commitment in our data is first-response time
            plan = plan_for(start)
            raw_d = s[start:end]
            before = s[max(0, start - 20):start]
            if topic == "response":
                if plan not in data.plans:
                    add(HOLD, f"Response time “{raw_d}”: the price sheet has no first-response time for {plan or 'an unnamed plan'}.", raw_d)
                    continue
                hours, truth = to_days(n, unit) * 24, data.response_hours(plan)
                if abs(hours - truth) < 0.01:
                    add(OK, f"{plan} first response {raw_d} matches the price sheet ({truth:g} hours).", raw_d)
                else:
                    add(BLOCK, f"{plan} first response is {truth:g} hours; the email says {raw_d}.", raw_d)
            elif topic == "trial":
                days = to_days(n, unit)
                if plan in data.plans and data.trial(plan) == 0:
                    add(BLOCK, f"{plan} has no free trial; the email offers a {raw_d} trial.", raw_d)
                elif plan == "Enterprise":
                    add(BLOCK, f"Free trials are only for {', '.join(data.trial_plans)}.", raw_d)
                elif abs(days - data.trial_days) > 0.01:
                    add(BLOCK, f"The free trial is {data.trial_days} days; the email says {raw_d}.", raw_d)
                else:
                    add(OK, f"Free trial {raw_d} matches policy ({data.trial_days} days{', ' + plan if plan else ''}).", raw_d)
            elif topic == "refund":
                annual = bool(ANNUAL_PLAN.search(s)) and not MONTHLY_PLAN.search(s)
                truth = data.refund_annual if annual else data.refund_monthly
                days = to_days(n, unit)
                kind = "annual" if annual else "monthly"
                if abs(days - truth) > 0.01:
                    add(BLOCK, f"Refunds on {kind} plans are allowed within {truth} days of the first payment; the email says {raw_d}.", raw_d)
                else:
                    add(OK, f"Refund window {raw_d} matches the {kind}-plan policy ({truth} days).", raw_d)
            elif topic == "tenure":
                months = to_days(n, unit) / 30 if not unit.lower().startswith("year") else n * 12
                if tenure is not None and addressed:
                    claim_ok = tenure >= months if MORE.search(before) or re.search(r"\bover\b|more than|at least", before, re.I) else abs(tenure - months) < 1
                    if claim_ok:
                        add(OK, f"Tenure claim “{raw_d}” matches the customer's {tenure:g} months.", raw_d)
                    else:
                        add(BLOCK, f"The email says the customer has been with us {raw_d}; their tenure is {tenure:g} months.", raw_d)

        # refunds with no number: "any time", "within the first year"
        if "refund" in has and not durs and (FOREVER.search(s) or A_YEAR.search(s)):
            annual = bool(ANNUAL_PLAN.search(s)) and not MONTHLY_PLAN.search(s)
            truth = data.refund_annual if annual else data.refund_monthly
            span = (FOREVER.search(s) or A_YEAR.search(s)).group(0)
            add(BLOCK, f"Refunds on {'annual' if annual else 'monthly'} plans are allowed only within {truth} days of the first payment; "
                       f"the email says “{span}”.", span)
        if "trial" in has and card_required_claim(s) and not data.card_required:
            add(BLOCK, "The free trial does not require a card; the email says it does.", card_required_claim(s).group(0))
        if "trial" in has and not durs:
            for x, p in plans_here:
                if p in data.plans and data.trial(p) == 0 and re.search(r"\b(free trial|trial)\b", s, re.I) and not re.search(r"\bno\b|n't|not", s, re.I):
                    add(BLOCK, f"{p} has no free trial.", p)

        # ---- promises agents can't make (rule 3)
        promise = promise_claim(s)
        if promise:
            add(HOLD, f"Promises a feature, date, or exception that an agent can't commit to; a manager should review: “{raw}”",
                promise)

        if plans_here:
            last_plan = plans_here[-1][1]
        out.extend(checks)
        levels = {c.level for c in checks}
        coverage.append(OK in levels and levels == {OK})
    return out, coverage
