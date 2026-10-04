# SpaceX hierarchy response recovery

## Failure and reproduction

The first real shared SpaceX import produced one page chunk spanning all 407
pages. Page-title detection found 836 headings, but fine hierarchy sent its
deduplicated candidates in one request with a fixed 2,000-token completion
budget. The returned JSON ended midway through candidate 154. The generic
response parser returned raw text; hierarchy parsing interpreted that as no
levels and retained Root. The import was then marked successful.

The regression contract feeds a 407-page coarse leaf through real candidate
collection, prompt construction and skeleton refinement, using a provider
boundary that truncates its response at the supplied budget. Before the fix,
`test_refine_large_leaf_preserves_headings_when_provider_output_is_truncated`
failed with `assert 1 == 407`. After the fix it preserves all 407 sections.

## Change

`PageHierarchyLevelResolver` bounds candidate batches by the completion budget,
with at most 64 rows. It supplies the preceding ancestor chain with its assigned
relative levels so a batch boundary does not reset nesting. Every requested ID
must have an integer level; level 0 explicitly marks filtered noise. Duplicate,
unknown, missing or invalid rows and malformed JSON are rejected.

An invalid batch retries in smaller batches with the same hierarchy context.
An invalid singleton retries once, then fails the import. The current published
revision remains available; a malformed hierarchy is never treated as successful
Root fallback. Valid all-noise output can still retain the coarse section.

Refinement also retains the coarse parent when body pages precede the first
observed heading. Assembly can then preserve those pages alongside the refined
children. A separate contract reproduced missing opening pages before this fix.

## Verification

All 83 page-memory contracts passed, including truncation, incomplete-array
recovery, cross-batch nesting, explicit noise, exhausted recovery and opening
body-page preservation. Ruff and Pyright passed. The real HTTP/Worker update
published 502 page chunks covering all 407 pages, including opening body pages,
with no section spanning more than ten pages. The source document ID remained
stable and its old revision remains readable. Full Worker time was 43 minutes
5 seconds; hierarchy refinement was 26.217 seconds.

Follow-up agent validation exposed a separate context issue: successful reads
were compacted after two turns, causing repeated reads of complementary
sections. The OpenAI harness now retains successful evidence with refs while
compacting stale discovery/error messages. A full-episode provider contract
reproduced the loop before this change and finishes in four turns afterward.
Both real reader launch-comparison queries finished with precise page ranges.
See [the validation report](shared-demo-validation-20261003.md) for timings and
the exact verification boundary.
