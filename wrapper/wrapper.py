"""Your wrapper. Start with check_source_of_truth() and policy.json.

Flow: call Ezio -> read its signals -> check your persona's source of truth -> apply policy
-> decision (+ reason, evidence) -> audit log -> run the mock action only if approved.

Run one input:  python3 wrapper.py customer_support "Your 30% loyalty discount applies to the Pro plan."
Score yourself: python3 evaluate.py customer_support
"""
import csv
import json
import math
import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import audit
import fact_check
import support_check
import mock_actions
from ezio_client import EzioError, analyze

HERE = Path(__file__).resolve().parent
PERSONAS = HERE.parent / "personas"
POLICY = json.loads((HERE / "policy.json").read_text())
fact_check.PCT_TOL = POLICY["fact_sheet_tolerances"]["percent_points"]
fact_check.USD_TOL = POLICY["fact_sheet_tolerances"]["dollar_relative"]

APPROVE, HOLD, BLOCK = "APPROVE", "HOLD_FOR_REVIEW", "BLOCK"
RANK = {APPROVE: 0, HOLD: 1, BLOCK: 2}


@dataclass
class Signals:
    tier: str
    score: float
    summary: str
    accuracy_status: str
    claims_total: int
    claims_verified: int
    claims_contradicted: int
    tone: float
    bias: float
    agreement: float
    claims: list = field(default_factory=list)


@dataclass
class Finding:
    level: str          # BLOCK, HOLD_FOR_REVIEW, or OK (verified; evidence only)
    message: str
    source: str = "fact_sheet"   # fact_sheet | ezio | tone_rules
    claim: str = ""              # text span the finding is about (for the UI)


@dataclass
class Decision:
    decision: str
    reason: str
    evidence: list
    ezio: dict
    findings: list = field(default_factory=list)   # structured findings (level, message, source, claim)
    signals: dict = field(default_factory=dict)    # Ezio signals the policy used


def parse_signals(resp):
    try:
        verdict, dims = resp["verdict"], resp["dimensions"]
        acc = dims["accuracy"]
        if verdict["tier"] not in ("GREEN", "YELLOW", "RED") or acc["status"] not in ("scored", "not_scored", "unavailable"):
            raise ValueError("invalid tier or accuracy status")
        scores = [verdict["score"]] + [dims[d]["score"] for d in ("tone", "bias", "agreement")]
        if acc["status"] == "scored":
            scores.append(acc["score"])
        if any(isinstance(s, bool) or not isinstance(s, (int, float)) or not math.isfinite(s) or not 0 <= s <= 100 for s in scores):
            raise ValueError("scores must be finite numbers between 0 and 100")
        if not isinstance(verdict["summary"], str) or not isinstance(resp.get("claims", []), list):
            raise ValueError("invalid summary or claims")
        return Signals(
            tier=verdict["tier"], score=verdict["score"], summary=verdict["summary"],
            accuracy_status=acc["status"], claims_total=acc.get("claims_total", 0),
            claims_verified=acc.get("claims_verified", 0), claims_contradicted=acc.get("claims_contradicted", 0),
            tone=dims["tone"]["score"], bias=dims["bias"]["score"], agreement=dims["agreement"]["score"],
            claims=resp.get("claims", []),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise EzioError("malformed mock response", resp) from e


@lru_cache(maxsize=None)
def load_source_of_truth(persona):
    """Every .json and .csv file in the persona folder, keyed by file name (for example sot['policies'])."""
    data = {}
    for f in sorted((PERSONAS / persona).iterdir()):
        if f.stem in ("practice_set", "business_rules"):
            continue
        if f.suffix == ".json":
            data[f.stem] = json.loads(f.read_text())
        elif f.suffix == ".csv":
            with f.open() as handle:
                data[f.stem] = list(csv.DictReader(handle))
    return data


MASH_WORD = re.compile(r"^[^aeiouy]+$|[bcdfghjklmnpqrstvwxz]{5,}|(.)\1\1", re.I)
PLACEHOLDER = re.compile(r"lorem ipsum|\[\s*insert|\bTODO\b|\bTBD\b|<\s*(?:name|customer|insert)[^>]*>|\{\{", re.I)
FILLER = {"test", "testing", "asdf", "blah", "foo", "bar", "baz", "lorem", "ipsum", "dolor", "sit", "amet", "xxx", "abc", "qwerty", "hello", "hi"}


def readability_findings(text):
    """Input sanity check (any persona): gibberish has no checkable claims and Ezio scores it GREEN 100,
    so without this it would be approved and sent. Keyboard mash or placeholder text is held for a person."""
    out = []
    words_all = [w.lower() for w in re.findall(r"[A-Za-z]+", text)]
    if PLACEHOLDER.search(text) or (words_all and all(w in FILLER for w in words_all) and set(words_all) - {"hello", "hi"}):
        return [Finding(HOLD, "The draft contains placeholder or test text, so it can't be sent automatically.", "input_check",
                        (PLACEHOLDER.search(text).group(0) if PLACEHOLDER.search(text) else words_all[0]))]
    # split only after a real word (not "U.S.", "Co.", "v."), so citations aren't cut into fragments
    for sentence in re.split(r"(?<=[a-z]{3}[.!?])\s+|\n+", text.strip()):
        words = [w.split("'")[0] for w in re.findall(r"[A-Za-z']+", sentence)]   # "you're" -> "you"
        words = [w for w in words if len(w) > 2]
        mash = [w for w in words if MASH_WORD.search(w) and not w.isupper()]   # GDPR, SSO, CSV are fine
        if words and len(mash) >= max(1, len(words) * 0.4):
            sample = sentence if len(sentence) <= 80 else sentence[:77] + "..."
            out.append(Finding(HOLD, f"This doesn't read as a coherent message, so it can't be sent automatically: \u201c{sample}\u201d",
                               "input_check", sample))
    return out


def check_source_of_truth(text, context, sot, signals=None):
    """Independent signal: every figure is compared with the persona's source of truth.

    Returns (findings, fully_verified). fully_verified means every sentence had at least one figure
    verified against the fact sheet and nothing unverified, which is what lets us approve when
    Ezio's fact-check is unavailable.
    """
    supported = [c.get("text", "") for c in (signals.claims if signals else []) if c.get("verdict") == "supported"]
    if "fact_sheet" in sot:            # financial_analyst
        checks, coverage = fact_check.check(text, sot["fact_sheet"], supported)
        source, promo, reviewer = "fact_sheet", fact_check.promotional(text), "an analyst's review before it reaches a client"
    elif "policies" in sot and "price_sheet" in sot:   # customer_support
        checks, coverage = support_check.check(text, context, sot["policies"], sot["price_sheet"], supported)
        source, promo, reviewer = "price_sheet/policies", support_check.promotional(text), "a manager's review before it goes to the customer"
    else:
        return [], False
    findings = [Finding(c.level, c.message, source, c.claim) for c in checks] + readability_findings(text)
    if promo:
        findings.append(Finding(HOLD, f"Promotional or one-sided wording needs {reviewer}: "
                                + ", ".join(f"\u201c{w}\u201d" for w in promo) + ".", "tone_rules", promo[0]))
    return findings, bool(coverage) and all(coverage)


def ezio_findings(signals):
    """Per-claim reading of Ezio's fact-check: a contradicted claim blocks, unverifiable holds, supported is evidence."""
    out = []
    for c in signals.claims:
        if not isinstance(c, dict):
            continue
        verdict, text = c.get("verdict"), c.get("text", "")
        src = f" (source: {c['source']})" if c.get("source") else ""
        if verdict == "contradicted":
            out.append(Finding(BLOCK, f"Ezio fact-check contradicts: \u201c{text}\u201d{src}", "ezio", text))
        elif verdict == "unverifiable":
            out.append(Finding(HOLD, f"Ezio could not verify: \u201c{text}\u201d", "ezio", text))
        elif verdict == "supported":
            out.append(Finding("OK", f"Ezio fact-check supports: \u201c{text}\u201d{src}", "ezio", text))
    if signals.claims_contradicted and not any(f.level == BLOCK for f in out):
        out.append(Finding(BLOCK, f"Ezio reports {signals.claims_contradicted} contradicted claim(s).", "ezio"))
    return out


def apply_policy(signals, findings, policy, fully_verified=False):
    decision, reasons = APPROVE, []

    def raise_to(level, reason):
        nonlocal decision
        reasons.append(reason)
        if RANK[level] > RANK[decision]:
            decision = level

    for f in findings:
        if f.level in RANK and f.level != APPROVE:
            raise_to(f.level, f.message)
    for dim, rule in policy["dimension_minimums"].items():
        score = getattr(signals, dim)
        if score < rule["hold_below"]:
            raise_to(HOLD, f"Ezio {dim} score is {score:g}, below our minimum of {rule['hold_below']}")
    if signals.accuracy_status == "not_scored" and policy["when_accuracy_not_scored"] != "ignore":
        raise_to(policy["when_accuracy_not_scored"], "accuracy was not scored (no claims checked)")
    if signals.accuracy_status == "unavailable":
        if policy["when_accuracy_unavailable"] == "approve_if_fully_verified":
            if not fully_verified:
                raise_to(HOLD, "Ezio's fact-check was unavailable and the fact sheet could not independently verify every sentence")
        else:
            raise_to(policy["when_accuracy_unavailable"], "Ezio's fact-check was unavailable")
    tier_decision = policy["tier_decisions"][signals.tier]
    if tier_decision != APPROVE:
        raise_to(tier_decision, f"Ezio overall tier is {signals.tier} ({signals.score:g})")
    return reasons, decision


def summarize(decision, reasons, verified):
    if decision == APPROVE:
        lead = "Approved: safe to send."
        if verified:
            lead += f" {len(verified)} figure(s)/claim(s) independently verified; no contradictions, promotional language, or weak Ezio dimensions."
        else:
            lead += " No checkable figures or claims, and Ezio found no tone, bias, or agreement problems."
        return lead
    lead = {BLOCK: "Blocked: the draft contradicts the source of truth and the action must not run.",
            HOLD: "Held for review: something in the draft cannot be confirmed, is a promise an agent can't make, or reads as promotional."}[decision]
    return lead + " " + "; ".join(dict.fromkeys(r.rstrip(".") for r in reasons)) + "."


def signal_summary(signals):
    return {"tier": signals.tier, "score": signals.score, "accuracy_status": signals.accuracy_status,
            "claims_total": signals.claims_total, "claims_verified": signals.claims_verified,
            "claims_contradicted": signals.claims_contradicted,
            "tone": signals.tone, "bias": signals.bias, "agreement": signals.agreement}


def decide(text, context=None, persona="customer_support", scenario=None, audit_log=True):
    context = context or {}
    sot = load_source_of_truth(persona)
    try:
        response = analyze(text, scenario=scenario)
        signals = parse_signals(response)
    except EzioError as e:
        # Ezio is down or malformed. Our own check still runs: a proven contradiction is blocked regardless;
        # anything else is held, because tone, bias, and public-claim checks are missing.
        findings, _ = check_source_of_truth(text, context, sot)
        blocks = [f for f in findings if f.level == BLOCK]
        decision = BLOCK if blocks else POLICY["on_ezio_failure"]
        why = (f"Ezio unavailable ({e}). " + ("The fact sheet independently shows: " + "; ".join(f.message for f in blocks)
               if blocks else "Without Ezio's tone, bias, and fact-check signals the note cannot be cleared automatically."))
        evidence = [f"Ezio error: {e}"] + [f"[{f.level}] {f.message}" for f in findings]
        result = Decision(decision, why, evidence, {"error": str(e), "response": e.response},
                          [vars(f) for f in findings], {"ezio_error": str(e)})
    else:
        findings, fully_verified = check_source_of_truth(text, context, sot, signals)
        findings += ezio_findings(signals)
        reasons, decision = apply_policy(signals, findings, POLICY, fully_verified)
        verified = [f for f in findings if f.level == "OK"]
        evidence = [f"[{'VERIFIED' if f.level == 'OK' else f.level}] {f.message}" for f in findings]
        evidence.append(f"Ezio: tier {signals.tier} {signals.score:g}, accuracy {signals.accuracy_status}, "
                        f"tone {signals.tone:g}, bias {signals.bias:g}, agreement {signals.agreement:g}")
        result = Decision(decision, summarize(decision, reasons, verified), evidence, response,
                          [vars(f) for f in findings], signal_summary(signals))
    if audit_log:
        audit.record(persona, text, context, result)
    return result


def gate(text, context=None, persona="customer_support"):
    result = decide(text, context, persona)
    print(f"{result.decision}: {result.reason}")
    for line in result.evidence:
        print(f"  - {line}")
    if result.decision == APPROVE:
        mock_actions.run_for_persona(persona, text)
    else:
        print("[GATE] action not executed")
    return result


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit('usage: python3 wrapper.py <persona> "<text>" [context-json]')
    gate(sys.argv[2], json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}, sys.argv[1])
