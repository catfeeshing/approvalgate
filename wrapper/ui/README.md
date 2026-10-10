# Persona page: outline

`persona.html` is one offline file. It works for all three personas. Choose one with `?persona=` (default `customer_support`), or run `python3 gate.py serve --persona customer_support` and open the address it prints.

## Generic parts (shared by every persona)

| Part | What it does | Depends on |
|---|---|---|
| Draft input | Textarea with a 20,000-character counter and client-side validation | `/decide` limits |
| Context fields | Built from the persona's `context` list (optional) | Persona config |
| Decision badge | Text, icon, and color together (never color alone) | `decision` |
| Consequence line | What happens next, per decision | Persona config |
| Action status | Ran / did not run | `action_executed` |
| Reason and findings | Findings list with a Block / Hold / Verified tag; falls back to `evidence` | `reason`, `findings`, `evidence` |
| Highlighted draft | Marks each finding's `claim` span in the text (display only) | `findings[].claim` |
| Ezio signals | Overall tier, accuracy status, tone, bias, agreement | `signals` (or `signals.ezio_error`) |
| Review step | Shown for HOLD. Keep on hold, reject, or revise and re-check, each with a required reason. Every review is written to the audit trail. Revise and re-check runs the edited draft through the gate, which alone decides | `POST /api/review`, then `POST /decide` |
| Next step | One line per decision saying what the user does next, naming the persona's reviewer | Persona config |
| Ezio signals | Full panel when the signals can explain the decision (not a clean approve, a non-GREEN tier, or unavailable accuracy); otherwise a one-line summary | `signals` |
| Samples | Pick one, simulate it, or run all in a table with decision, action, and reason per sample, and a summary of how many held or blocked inputs reached the action (should be 0) | `GET /api/samples` |
| Audit trail | Recent decisions and reviews for this persona, newest first | `GET /api/audit` |
| Errors | Unreachable wrapper, HTTP errors, unreadable responses, 70-second timeout | Network |

The page never decides, calls the mock, or runs the action. Every decision comes from `POST /decide`.

## Customer service configuration

The `customer_support` entry in `PERSONAS` in `persona.html` contains:

- **Copy:** title, subtitle, draft label, and what "ran" and "did not run" mean (an email sent to a customer).
- **Consequences:** approve sends now; hold waits for a manager; block means it contradicts the price sheet or policy.
- **Context fields:** `plan` (Starter, Pro, Business) and `customer_tenure_months`. These match `personas/customer_support/practice_set.json`.

To add a persona, add an entry with the same keys. No other code changes are needed.

## Merged customer support wrapper (checked)

The customer support check (`wrapper/support_check.py`, merged in `d7d3dbc`) works with the page unchanged. What was checked:

1. **Persona name.** `serve.py` accepts `customer_support`, and the page defaults to it.
2. **Context keys.** The checker reads `plan` and `customer_tenure_months`, which match the page's context fields.
3. **Response shape.** `findings[]` now have `source` values of `price_sheet/policies`, `tone_rules`, or `ezio`. The page doesn't use `source`, so no change was needed. `claim` is set for every fact-sheet and price-sheet finding, and it matches the draft text in all but a few Ezio paraphrases.
4. **Finding levels.** `BLOCK`, `HOLD_FOR_REVIEW`, and `OK`, as expected.
5. **Ezio signals.** Unchanged.
6. **Practice set.** `evaluate.py customer_support` gives 16/16 exact, 8/8 caught, 0/8 false blocks.
7. **End to end.** A 30% loyalty claim for an 18-month Pro customer was blocked against the 15% price sheet, with the claim span `30%`. Samples, review recording, and the page all work.

Ongoing:

- **Review outcomes** are recorded only. `HOLD_CONFIRMED` and `REJECTED` record a decision and send nothing. `REVISED` records the reviewer's reason and then re-checks the edited draft through `/decide`, so a revised email goes out only if the gate approves it. Releasing a held item without a new APPROVE is deliberately not supported: `gate.py judge` requires the mock action to run exactly when the decision is APPROVE, so a review-triggered send would fail verification.
7. **Audit endpoints.** `serve.py` provides `GET /api/audit` (recent decisions, with the full Ezio response left out) and `POST /api/review`. The `audit.py` functions `recent()` and `record_review()` are the backend for them.
8. **Where reviews go.** Reviews are written to `reviews.jsonl`, next to `audit_log.jsonl`. They are kept out of the decision log so each decision entry stays unchanged for `gate.py`.

## Known limits

- The run-all table sends every sample through `/decide`. Approved samples run their mock action, the same as a real check.
- The page relies on the `findings[].claim` text appearing verbatim in the draft to highlight it. Ezio paraphrases are not highlighted.
- The page does not poll. A stalled request is shown as a timeout after 70 seconds, and the user is told to check the audit log before retrying, because the wrapper may have finished.
- The audit view shows the last 50 decisions and 50 reviews. It reads the whole file on each request, which is fine for a demo but not for a large log.
- The audit view is filtered to the page's persona. A log shared by several personas shows only this one.
