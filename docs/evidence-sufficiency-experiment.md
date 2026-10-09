# Evidence-sufficiency finish-gate experiment

`scripts/experiment_evidence_review.py` executes paired baseline/review episodes
through the production `OpenAIHarness`, `EvidenceReviewSession`, evidence
composition, and `build_review_snapshot`. It exercises the actual finish gate,
correction continuation, final-packet validation, strict verdict parser, and
shared step/token accounting. Gold metrics are calculated from the resulting
selected source IDs after the runtime completes.

The corpus backend and explorer transport are scripted. In the default mode,
reviewer responses are also predetermined. This is a reproducible runtime
integration experiment, **not evidence of real-model retrieval gains**. It does
not exercise PostgreSQL search ranking, database authorization, or network
inference. Harness and route tests cover those integration contracts separately.

## Run the controlled experiment

With the repository's Python dependencies installed:

```bash
python scripts/experiment_evidence_review.py \
  --repeats 3 --output /tmp/evidence-review-controlled.json
```

No service or model credentials are needed. The script does not connect to a
database or object store. It prints a compact summary and exits nonzero if a
runtime gate fails. Run one case with `--case missing_table`; repeat `--case` to
select several.

Each pair uses the same frozen corpus, query, scripted explorer, initial step
limit, and wall-clock limit. Pair order alternates across cases/repeats. Both
arms initially discover/read/pick the same evidence. Only an actual continuation
from the production finish-review policy exposes a second scripted discovery
page. Neither explorer nor corpus backend reads gold annotations. Reviewer
fixtures are fixed JSON outputs, with source aliases translated to production
snapshot IDs; the experiment never computes a verdict from gold coverage.

The usual initial budget is 15 exploration/review steps and 180 seconds.
Reviewer calls consume the same budget and add their usage to episode tokens.
One corrective cycle can tighten the remaining budget to four steps; it does
not add budget. Dedicated cases start with an exhausted step or time budget.
Pick turns retain the existing harness semantics: they consume tokens but do
not consume an exploration step.

The report includes fixture/script/runtime SHA-256 digests, Git base revision,
Python version, pair order, final source IDs, source snapshots, trace actions,
verdict metadata, per-case gates, known token usage, and episode duration.
Changing source code without committing is captured by the runtime digests.

## Metrics and limits

| Metric | Definition |
|---|---|
| Gold evidence coverage | Fraction of annotated requirement groups with at least one selected acceptable source. Averaged over answerable cases; no-answer coverage is null. |
| Gold sufficient | Every requirement group covered **and** the frozen corpus is annotated answerable. Having both sides of an unresolved contradiction is not sufficient. |
| Baseline incomplete finish | An answerable case ending `finished` without sufficient gold evidence. This describes a retrieval stop, not an answer or a sufficiency claim. |
| False sufficient | Review status `sufficient` when gold sufficiency is false. Reported both per insufficient final packet and as a fraction of sufficient claims. The baseline has no sufficiency verdict, so its false-sufficiency rate is null. |
| Unanswerable rejection | A known unanswerable fixture whose final review is `insufficient`. This does not mean the runtime has proved that an arbitrary corpus has no answer. |
| Usage | Explorer, reviewer, and summed episode tokens, including both judge calls. Unknown usage is counted separately, not silently treated as complete zero usage. |
| Latency | `perf_counter` duration of the actual episode, plus finish-review trace duration. Default-mode numbers are local orchestration timings, not LLM latency. |

Runtime gates assert: no review calls or snapshot loads in the disabled arm;
at most two review calls and one corrective retrieval; enabled total step cap;
source-only reviewer prompts; known usage accounting; expected controlled
failure/status transitions; and no explorer finish note or reviewer prose in
composed evidence.

The negative-control case deliberately supplies a schema-valid but semantically
wrong reviewer assessment: it omits a requested facet and claims sufficiency.
The runtime cannot prove from JSON shape or exact quotations that all facets
were derived correctly. The evaluator must detect this false sufficient event.
It is reported separately and is an expected outcome, not a failed runtime
gate. This case demonstrates why semantic quality needs a real-model evaluation.

## Controlled run recorded during implementation

Command above, Python 3.12.14, 14 cases × 3 repeats × 2 arms: **84 episodes, zero
failed runtime gates**. Repeated results are identical except for timing. No live
model run was performed because model credentials were not configured.

| Case | Baseline → review coverage | Final review | Review calls | Corrections |
|---|---:|---|---:|---:|
| Complete control | 100% → 100% | sufficient | 1 | 0 |
| Multiple requested dimensions | 50% → 100% | sufficient | 2 | 1 |
| Missing comparison side | 50% → 100% | sufficient | 2 | 1 |
| Missing numeric table; misleading summary | 0% → 100% | sufficient | 2 | 1 |
| Unresolved conflicting bulletins | 100% → 100% | insufficient | 2 | 1 |
| Outdated policy; current revision available | 0% → 100% | sufficient | 2 | 1 |
| No answer in the frozen corpus | n/a | insufficient | 2 | 1 |
| Empty initial pool | 0% → 100% | sufficient | 1 | 1 |
| Reviewer outage | 50% → 50% | unverified | 1 | 0 |
| Malformed reviewer JSON | 50% → 50% | unverified | 1 | 0 |
| Exhausted step budget | 50% → 50% | unverified | 0 | 0 |
| Exhausted wall clock | 0% → 0% | unverified | 0 | 0 |
| Still incomplete after one correction | 33.3% → 66.7% | insufficient | 2 | 1 |
| Intentionally false-sufficient negative control | 50% → 50% | sufficient, wrong | 1 | 0 |

Across the 12 answerable cases, macro gold coverage is 36.1% → 72.2%, and fully
covered cases are 1/12 → 6/12. These are **engineered recovery opportunities with
scripted decisions**, not measured model improvements. The two unanswerable
fixtures both remain insufficient. All three repeats detect the intentional
false-sufficiency negative control; there are no other false-sufficient events
in these predetermined outputs.

Per full 14-case pass, baseline invokes zero reviewers; the review arm invokes
19. Across three repeats, known simulated token totals are 1,989 baseline and
5,580 review, including 2,538 reviewer tokens. Explorer responses report 13
tokens and valid/malformed reviewer responses report 47 tokens each. The outage
fixture reports unknown reviewer usage, so the review total is explicitly
incomplete for three runs. Local mean episode times in the recorded run were
approximately 0.92 ms baseline and 1.87 ms review; these have no predictive value
for real provider latency or cost.

## Optional live reviewer evaluation

After configuring the normal direct text provider (`DS_KEY`, `DS_URL`) and
disabling `LLM_MOCK_ENABLED`, run:

```bash
python scripts/experiment_evidence_review.py --mode live-reviewer \
  --repeats 3 --output /tmp/evidence-review-live.json
```

This explicitly uses the production reviewer adapter/model while keeping the
explorer and corpus controlled. The outage, malformed-response, and intentional
false-sufficiency fixtures are excluded because they require scripted reviewer
behavior. The other runtime gates still run; expected scripted verdicts are not
asserted. Live reviewer usage is provider-reported, while explorer tokens remain
simulated. Keep these results separate from the default controlled report.

For a production quality claim, additionally use a held-out representative
corpus/query set, freeze source revisions and gold annotations before reviewing
outputs, and run both arms with the same real explorer model, provider settings,
scope, and total budget. Report failure/unknown-usage rates, false-sufficiency and
unanswerable performance alongside coverage and cost/latency distributions. This
script alone does not establish those claims.
