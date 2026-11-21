# Knowhere Retrieval

Knowhere makes document revisions available for retrieval within an owner's
namespace. These terms describe document availability and changes to the
retrieval service.

## Language

**Document**:
A document with a stable identity within its owner's namespace. Parsing
publishes a new document. It does not replace the content of an existing
document. Different content is a new document; deleting the previous
document is the caller's own action.
_Avoid_: Parsing job, source filename, in-place update

**Archived Document**:
A document excluded from new retrievals by archival. A retrieval already
in progress may use bodies it read before archival committed. Ongoing work
cannot return the document to the searchable corpus. Retained parsing results
and processing history follow existing retention rules.
_Avoid_: Failed parsing job, permanently deleted data

**Document Revision**:
The one immutable parsing result of a document. The baseline and later
enrichment are representations of this same result.
_Avoid_: Separate document, in-place replacement, indexing retry

**Retrieval Unit**:
A piece of a document revision considered as one candidate during lexical
discovery, with its own identity and associated content and path. Its count
is distinct from the number of source pages or words in that revision.
_Avoid_: Word, page, whole document

**Published Revision**:
The document revision whose baseline index is available for retrieval.
Enrichment does not publish another revision in its place.
_Avoid_: Latest parsed revision, activated replacement

**Latest Parsed Revision**:
The document's stored parsing result. Workers can retain it before initial
indexing finishes. A retained result from a failed ingestion remains internal.
_Avoid_: Latest submitted revision, a second revision of the same document

**Primary Lexical Backend**:
The selected service that supplies lexical retrieval. Publishing the
initial index to this backend is required for Parsing Completion.
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

**Parsing Completion**:
The sole successful outcome visible to a caller for a document-processing
job. The initial parsing result and its searchable index are available.
Full ingestion publishes the complete result. Progressive ingestion
publishes the baseline. A new progressive PDF or PPTX baseline includes
coarse chapters and an initial directory.
_Avoid_: Search completion, indexing completion, enrichment completion

**Indexing**:
The phase of a document-processing job that makes an already parsed revision
available for retrieval. Initial indexing is part of the ingestion job.
_Avoid_: Parsing in progress, separate indexing job

**Job Termination**:
The end of a document-processing job through overall success or failure.
Retrying failed ingestion starts a new job.
_Avoid_: Job success, parsing completion

**Client Wait Satisfaction**:
The client observing Parsing Completion for the job it is waiting on.
The wait observes the job's recorded outcome.
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
or archival before publication completes. An initial indexing failure ends
the ingestion job and keeps any retained parsing output internal.
_Avoid_: Parsing failure, job completion

**Ingestion Retry**:
An explicit resubmission of the original source after a failed ingestion.
The resubmission creates a new ingestion job.
_Avoid_: Indexing retry, reactivation, in-place document update

**Indexing Attempt**:
One initial indexing cycle for a parsed revision, with at most one execution
belonging to the same job and parsing result. Repeated delivery of the
processing request refers to the same attempt.
_Avoid_: Automatic retry cycle, new parsing job, webhook delivery attempt

**Indexing Execution**:
One complete processing round within an indexing attempt, beginning before
connecting to or opening the index and ending in committed publication or an
unsuccessful outcome. A round can fail before any records are written.
_Avoid_: Write-only attempt, claimed writer execution, individual bulk request

**Lexical Build**:
The lexical records produced by one indexing execution for one representation
of a document revision. The baseline build and an enrichment build of the
same revision can both supply candidates. Coverage helps assembly omit
baseline hits that have clear replacements in the same retrieval result.
_Avoid_: Document revision, namespace index, indexing attempt

**Generated Result Package**:
The ZIP fixed at a job's one completion notice. Later on-demand enrichment
does not replace it.
_Avoid_: Retrieval body, enrichment unit, a second job result

**Document Baseline**:
The first published representation of a document revision. It can be retrieved
before enrichment of that revision finishes. For a new progressive PDF or
PPTX this is the coarse chapter representation.
_Avoid_: Enriched representation, a separate document

**Enrichment Failure**:
An unsuccessful on-demand enrichment of one unit. The job completion notice
stays in place, and that unit's baseline remains available for retrieval.
After failure cleanup, the baseline supplies new hits and previously returned
references still open their stored bodies.
_Avoid_: Job failure, a second completion notice

**Running Enrichment**:
The single on-demand attempt currently working on one enrichment unit. Its
baseline and any searchable enrichment can both supply hits during publication.
_Avoid_: A second enrichment body for this unit, a document-wide hold

**Enrichment Unit**:
The portion of a document revision parsed by one enrichment task. For a PDF
or PPTX it is one baseline page range. For a spreadsheet it is one sheet group.
For text, Markdown, Word, and standalone audio and video it is the whole
document.
_Avoid_: Chapter title, single chunk, the original parse job

**Live Document Read**:
A direct read of a document's stored bodies, including its chunk list. The
list can include both baseline and enrichment chunks from the same unit.
Adjacent sections can share a boundary page, and the page's content can
differ across representations.
_Avoid_: Generated Result Package, retrieval result, a duplicate created by enrichment

**Source Page Complement**:
Coverage by enrichment hits included in the same response. Those hits provide
searchable content for every original physical page of a baseline hit, which
allows assembly to drop that baseline hit. Source content carried forward
as a fallback counts.
_Avoid_: Chunk identity, chapter title, one shared page

**Failure Stage**:
The phase in which a document revision failed: parsing or indexing.
An indexing failure preserves the successful parsing outcome and its result.
_Avoid_: Undifferentiated document failure
