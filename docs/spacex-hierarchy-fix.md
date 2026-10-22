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

## Preserve the document prefix before anchored chapters

A separate production result omitted the pages before the first anchored
chapter: Meta's nine-page release contained only pages 5–9, and SpaceX's
407-page filing contained only pages 20–407. Resolving a TOC correctly did not
guarantee complete document coverage: only the resolved section ranges were
sent to rendering, tagging, and node assembly.

The document prefix is every physical page before the first recognized chapter,
including printed TOC pages. It belongs directly to the existing document root.
Its chunk path is the filename itself; no `filename/Root` chapter is inserted,
and existing chapter paths and ranges stay unchanged. Publication already maps
a filename-only chunk to its canonical `Root` section. Navigation retains its
existing root-body projection at the filename path.

The prefix has explicit `document_prefix` evidence so an isolated prefix scope
does not expand into the full-document Root fallback. It receives ordinary
rendering, page tagging, and full-page citation assets, but does not undergo
chapter-title refinement or single-child collapse. Prefix TOCs are retained
through production and staged replay; TOC policy inside chapters is unchanged.
The source evidence and level zero survive hierarchy artifact round trips.

This repair covers opening pages only. It does not fill arbitrary middle or
trailing gaps, regenerate existing production results, or change no-TOC
full-document fallback behavior. The historical full-provider validation above
is separate from the prefix-repair validation.

### Prefix-repair verification on 2026-10-22

Validation ran in an isolated Linux ARM64 environment, based on `main` commit
`2ed1889626f63240bd0894ec0c24b7a7c5217dfb`.

The new output contract failed for both missing-prefix examples before the
repair; the first-page chapter and no-TOC fallback controls already passed.
A separate staged-artifact contract reproduced the loss of level zero on
reload before its repair. The final focused run passed **102 contracts**:

```sh
.venv/bin/pytest apps/worker/tests/contract/test_page_memory* \
  apps/worker/tests/contract/test_profile_skeleton_anchor_wiring_contract.py -q
```

The full Worker contract suite also passed **405 contracts**, including the
database contracts that reload application modules between environments:

```sh
.venv/bin/pytest apps/worker/tests/contract -q
```

This includes direct-root text and full-page citation assets, TOC retention in
the prefix, unchanged chapter TOC policy, first-page chapters, no-TOC fallback,
filename-only publication/navigation projection, and staged artifact reload.
Ruff passed on the page-memory modules, changed stage scripts, and contracts;
Pyright passed on the five changed application modules with zero errors and
warnings.

Captured production anatomy was also replayed against the actual originals:

| Document | Real root-body output | Citation assets | Coverage evidence |
| --- | --- | --- | --- |
| Meta Q1 2026 Exhibit 99.1 | Filename path, physical pages 1–4, 11,265 text characters | Four real full-page PNGs | All nine pages assembled; five existing chapter paths retained |
| SpaceX S-1 | Filename path, physical pages 1–19, 24,086 text characters; printed TOC page 17 retained | Nineteen real full-page PNGs | New prefix plus 683 unchanged original body chunks covers all 407 pages |

The replay used real PDF rasterization, node assembly, and ZIP chunk-schema
conversion. It reused captured chapter anatomy and substituted page-tag
responses; it did not call model providers or publish a document revision.
SpaceX rendered only the missing prefix and reused the original body results,
rather than rerunning all 407 pages. Thus this is captured-profile seam
verification, not a fresh full-provider parse or production deployment.

### Existing demo revisions after deployment

Deploying the Worker repair does not regenerate existing parsed revisions.
After confirming the deployed Worker includes this change, reparse the known
affected Meta release and SpaceX filing through the maintainer demo ingestion
flow. Audit the remaining READY demos for missing opening pages before
selecting additional sources. The restoration helper supports source selection
and `--reparse-ready`; keep the previous revision available while an update runs.

Validate physical-page coverage, prefix TOC retention, filename-only root-body
paths, and page-citation assets on each new revision. Confirm that downstream
readers consume the new revision after publication.
