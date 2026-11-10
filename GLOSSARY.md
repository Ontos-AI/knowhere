# Knowhere Retrieval

Knowhere makes document revisions available for retrieval within an owner's
namespace. These terms describe document availability and changes to the
retrieval service.

## Language

**Document**:
A document with a stable identity within its owner's namespace. Replacing
its source file or parsing it again can produce a new revision without
changing that identity.
_Avoid_: Parsing job, source filename

**Archived Document**:
A document excluded from retrieval by archival. Ongoing work cannot return
it to the searchable corpus, and it is ineligible for indexing retry.
Retained parsing results and processing history follow existing retention
rules.
_Avoid_: Failed parsing job, permanently deleted data

**Document Revision**:
One immutable parsing result belonging to a document. Replacing the source
file or parsing it again produces another revision of the same document
when the operation explicitly targets that document.
_Avoid_: Separate document, indexing retry

**Retrieval Unit**:
A piece of a document revision considered as one candidate during lexical
discovery, with its own identity and associated content and path. Its count
is distinct from the number of source pages or words in that revision.
_Avoid_: Word, page, whole document

**Published Revision**:
The document revision currently selected for default retrieval. It remains
selected while a replacement is prepared and changes only when the
replacement is ready for retrieval and activated.
_Avoid_: Latest parsed revision

**Latest Parsed Revision**:
The newest successfully parsed revision of a document, selected for default
document-content reads even if its indexing is pending or failed. It may
differ from the published revision used for retrieval.
_Avoid_: Latest submitted revision, published revision

**Candidate Revision**:
A document revision targeted by the current update, awaiting search readiness
and activation. Its parsing result can already be available while an earlier
published revision remains selected for retrieval.
_Avoid_: Published revision, separate document

**Superseded Candidate**:
A candidate revision whose update has been replaced by a later accepted
update to the same document. It is no longer eligible for indexing retry or
activation, while its retained parsing result remains accessible.
_Avoid_: Published revision, retryable candidate

**Primary Lexical Backend**:
The selected service that supplies lexical retrieval. A candidate revision
becomes search-ready only after publication to this backend succeeds.
_Avoid_: Rollback copy, whichever backend responds first

**Migration Rollback Copy**:
A temporary lexical copy maintained for a deliberate return to the previous
backend during migration evaluation. Its completeness determines rollback
readiness, not successful publication to the current primary backend.
_Avoid_: Permanent backup, runtime fallback, primary publication

**Migration Rollback**:
A deliberate return to the previous retrieval backend while that backend is
temporarily retained for evaluating its replacement. It does not establish
a permanent backup or disaster-recovery source for the replacement.
_Avoid_: Runtime fallback, automatic backup, disaster recovery

**Migration Observation Period**:
The evaluation period after adopting a replacement retrieval backend, during
which real usage and operational behavior inform the stability review.
The temporary migration rollback path remains available subject to its
readiness requirements. Retiring that path follows a separate decision
after stability is confirmed.
_Avoid_: Automatic failover period, stability guaranteed by elapsed time

**Search Readiness**:
The state indicating whether a particular document revision is available for
retrieval. Completing document parsing alone does not establish search
readiness.
_Avoid_: Parsing completion, generic namespace health

**Parsing Completion**:
The milestone at which a document revision's parsing result is complete and
available to the caller for display or download. It does not require the
revision to be ready for retrieval.
_Avoid_: Job completion, search readiness

**Indexing**:
The phase of a document-processing job that makes an already parsed revision
ready for retrieval. It follows parsing within the same job; an indexing
failure does not undo parsing completion or create a separate processing job.
_Avoid_: Parsing in progress, separate indexing job

**Indexing Completion**:
The successful publication milestone at which a parsed document revision has
been activated for retrieval. Finishing index writes alone does not establish
this milestone. It remains a historical processing outcome if the revision
is later replaced or the retrieval service temporarily becomes unavailable.
_Avoid_: Parsing completion, current service availability

**Job Completion**:
The irreversible successful milestone at which a document revision's parsing
result is available and that revision is ready for retrieval.
_Avoid_: Parsing completion, job termination, client wait satisfaction

**Job Termination**:
The end of the current document-processing attempt, through overall success
or failure. A failed job may become active again through an eligible explicit
retry; successful job completion remains irreversible.
_Avoid_: Job success, parsing completion

**Client Wait Satisfaction**:
The caller reaching its selected document-processing milestone: parsing
completion or overall search-ready completion. It is specific to that wait
invocation and does not change the job's recorded outcome.
_Avoid_: Job completion, event subscription, worker-task completion

**Document Processing Job**:
One client-visible processing request for a document revision, spanning parsing
and indexing through completion or failure. Its background tasks and eligible
indexing retries retain the same job identity and processing history.
_Avoid_: Individual background task, parsing-only job, document revision

**Active Document Job**:
A document-processing job whose current execution has not reached overall
completion or terminal failure. Its activity includes waiting, parsing, and
indexing, even after parsing results become available.
_Avoid_: Running parser, parsing-only activity

**Indexing Failure**:
A final unsuccessful indexing or publication outcome for an already parsed
revision. It can result from an unsuccessful or interrupted indexing attempt,
or archival before publication completes. Its completed parsing result remains available.
_Avoid_: Parsing failure, job completion

**Indexing Retry**:
A free new indexing attempt for an eligible, already parsed candidate revision
explicitly requested by a client using its retained parsing result. It requires
neither reparsing nor another parsing charge and has no separate age-based
expiry while the revision remains eligible and its parsing result is available.
It cannot reactivate a superseded candidate or archived document.
_Avoid_: Reparse, document resubmission

**Indexing Attempt**:
One indexing cycle admitted initially or by an eligible explicit retry for a
parsed revision, with at most one execution belonging to the same job and parsing
result. Another attempt requires a new eligible explicit request; repeated
delivery of the same processing request does not create one.
_Avoid_: Automatic retry cycle, new parsing job, webhook delivery attempt

**Indexing Execution**:
One complete processing round within an indexing attempt, beginning before
connecting to or opening the index and ending in committed publication or an
unsuccessful outcome. A round can fail before any records are written.
_Avoid_: Write-only attempt, claimed writer execution, individual bulk request

**Lexical Build**:
The lexical records produced by one indexing execution for an immutable
document revision. Retried executions can produce distinct builds of the
same revision; only the selected published build supplies eligible search
hits. A retired build cannot become selected again.
_Avoid_: Document revision, namespace index, indexing attempt

**Failure Stage**:
The phase in which a document revision failed: parsing or indexing.
An indexing failure preserves the successful parsing outcome and its result.
_Avoid_: Undifferentiated document failure
