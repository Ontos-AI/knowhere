/** Exercise the built standalone Node package against an owned actual API server. */
import assert from 'node:assert/strict';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const [sdkRoot, baseURL, serializedJobs] = process.argv.slice(2);
const jobs = JSON.parse(serializedJobs);
const { Knowhere, APIError, JobFailedError, PollingTimeoutError } = await import(pathToFileURL(join(sdkRoot, 'dist/index.mjs')).href);
const client = new Knowhere({ apiKey: 'synthetic-http-key', baseURL, maxRetries: 3, timeout: 2000 });
// Read the body once; these requests never invoke the parsing admission route.
const request = async (path, method = 'GET') => {
  const response = await fetch(baseURL + path, { method });
  const body = await response.json();
  assert.equal(response.ok, true, JSON.stringify(body));
  return body;
};
const load = async (job) => {
  const result = await client.jobs.load(job, { verifyChecksum: false });
  assert.equal(result.chunks[0].content, 'Synthetic retained evidence');
  return result;
};

const running = await client.jobs.wait(jobs.running, { waitFor: 'parsed' });
assert.equal(running.status, 'running');
assert.equal(running.isDone, false);
assert.equal(running.parsingStatus, 'completed');
await load(running);
await assert.rejects(client.jobs.wait(jobs.running, { pollTimeout: 1, pollInterval: 1 }), (error) => error instanceof PollingTimeoutError && error.jobId === jobs.running && error.jobResult?.parsingStatus === 'completed');
const defaultWait = client.jobs.wait(jobs.running, { pollInterval: 5, pollTimeout: 2000 }).then(() => { throw new Error('Default wait returned before indexing'); }, (error) => error);
const early = await client.jobs.wait(jobs.running, { waitFor: 'parsed' });
assert.equal(early.isDone, false);
await request(`/test/fail/${jobs.running}`, 'POST');
const failure = await defaultWait;
assert.ok(failure instanceof JobFailedError);
assert.equal(failure.failureStage, 'indexing');
assert.equal(failure.jobResultId, running.jobResultId);
await load(failure.jobResult);
assert.equal((await client.jobs.wait(jobs.running, { waitFor: 'parsed' })).isFailed, true);

for (const [mode, status] of [['unavailable', 503], ['invalid', 500], ['timeout', 504], ['conflict', 409]]) {
  const before = await client.jobs.get(jobs[mode]);
  let failure;
  try {
    await client.jobs.retryIndexing(jobs[mode]);
    throw new Error('Expected canonical API failure');
  } catch (error) {
    failure = error;
  }
  assert.ok(failure instanceof APIError);
  assert.equal(failure.statusCode, status);
  assert.equal(failure.jobId, jobs[mode]);
  assert.equal(failure.jobResultId, before.jobResultId);
  assert.equal(failure.jobResult.parsingStatus, 'completed');
  assert.equal(failure.jobResult.jobId, jobs[mode]);
  if (status !== 409) assert.equal(failure.failureStage, 'indexing');
  await load(failure.jobResult);
  const after = await client.jobs.get(jobs[mode]);
  assert.equal(after.creditsSpent, before.creditsSpent);
  assert.equal((await client.jobs.wait(jobs[mode], { waitFor: 'parsed' })).isDone, false);
}
await assert.rejects(client.jobs.wait(jobs.unavailable), (error) => error instanceof JobFailedError && error.failureStage === 'indexing');
const earlyDelivery = await request(`/test/deliver-parsed/${jobs.completed}`, 'POST');
assert.equal(earlyDelivery.event, 'job.parsed');
assert.equal(earlyDelivery.status, 'running');
assert.equal(earlyDelivery.uniqueEvents, 1);
const ready = await client.jobs.retryIndexing(jobs.completed);
assert.equal(ready.isDone, true);
assert.equal(ready.indexingStatus, 'completed');
await load(ready);
assert.equal((await client.jobs.retryIndexing(jobs.completed)).jobResultId, ready.jobResultId);
const beforeEvents = await request('/test/metrics');
const delivery = await request(`/test/deliver/${jobs.completed}`, 'POST');
assert.deepEqual(delivery.deliveredNames, ['job.completed', 'job.parsed', 'job.parsed']);
assert.equal(delivery.uniqueEvents, 2);
assert.equal(delivery.events.find((event) => event.event === 'job.parsed').status, 'running');
assert.equal(delivery.events.find((event) => event.event === 'job.completed').status, 'completed');
assert.equal((await client.jobs.get(jobs.completed)).isDone, true);
await assert.rejects(client.jobs.retryIndexing(jobs['response-lost'], { timeout: 20 }), (error) => error.jobId === jobs['response-lost']);
assert.equal((await client.jobs.wait(jobs['response-lost'], { pollInterval: 5, pollTimeout: 2000 })).isDone, true);
const metrics = await request('/test/metrics');
for (const mode of ['unavailable', 'invalid', 'timeout', 'conflict', 'response-lost']) assert.equal(metrics.mutations[jobs[mode]], 1);
assert.equal(metrics.nativeWrites.completed, 1);
assert.equal(metrics.nativeWrites['response-lost'], 1);
assert.deepEqual(metrics.artifactAuthorization, []);
assert.deepEqual(metrics.delivered.map((event) => event.event), ['job.parsed', 'job.completed', 'job.parsed', 'job.parsed']);
assert.equal(metrics.nativeWrites.completed, beforeEvents.nativeWrites.completed);
console.log(JSON.stringify({ status: 'passed', sdk: 'node', statuses: [200, 409, 500, 503, 504], callbackDeliveries: 4, uniqueEvents: 2, retryMutationRequests: metrics.mutations, nativeWrites: metrics.nativeWrites }));
