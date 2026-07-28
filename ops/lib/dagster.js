/**
 * Dagster REST API client.
 * Uses fetch() to trigger jobs, poll run status, and list collections/items.
 *
 * @module dagster
 */

const DEFAULT_URL = process.env.DAGSTER_URL || 'http://localhost:3000';

/**
 * Encode a (collection, item) pair as a Dagster MultiPartitionKey string.
 * Dagster joins dimension keys sorted by dimension name with "|" — "collection" < "item"
 * alphabetically, so the encoding is `${collection}|${item}`. Verify against the installed
 * Dagster version once this bridge is actually implemented (phase 0.8).
 *
 * @param {string} collection - Collection partition key (format "[group]/[collection]").
 * @param {string} item - Item partition key (the item's ARK).
 * @returns {string} Encoded multi-partition key.
 */
function encodePartitionKey(collection, item) {
  return `${collection}|${item}`;
}

/**
 * Trigger a Dagster job run.
 *
 * @param {string} jobName - Name of the Dagster job to run.
 * @param {Object} [opts={}] - Options.
 * @param {string} [opts.baseUrl] - Dagster base URL override.
 * @param {Object} [opts.config] - Dagster run config to pass to the job.
 * @param {{collection: string, item: string}[]} [opts.partitionKeys] - Optional (collection, item) partition pairs to run.
 * @returns {Promise<Object>} Run object with at minimum a `runId` field.
 */
async function triggerJob(jobName, opts = {}) {
  const baseUrl = opts.baseUrl || DEFAULT_URL;
  const body = {
    repositoryLocationName: '__repository__',
    repositoryName: '__repository__',
    jobName,
    runConfigData: opts.config || {},
  };
  // TODO(phase 0.8): wire opts.partitionKeys into the launchRun mutation once the
  // Dagster GraphQL partition-run contract is confirmed against the installed version —
  // see encodePartitionKey() above for the key-encoding half of this.

  const res = await fetch(`${baseUrl}/graphql`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      query: `
        mutation LaunchRun($executionParams: ExecutionParams!) {
          launchRun(executionParams: $executionParams) {
            ... on LaunchRunSuccess { run { runId status } }
            ... on PythonError { message }
            ... on InvalidSubsetError { message }
          }
        }
      `,
      variables: { executionParams: body },
    }),
  });

  const data = await res.json();
  return data?.data?.launchRun?.run || data;
}

/**
 * Poll a Dagster run until it reaches a terminal state.
 *
 * @param {string} runId - Dagster run ID.
 * @param {Object} [opts={}] - Options.
 * @param {string} [opts.baseUrl] - Dagster base URL override.
 * @param {number} [opts.intervalMs=3000] - Poll interval in milliseconds.
 * @returns {Promise<Object>} Final run object with `runId` and `status`.
 */
async function pollRun(runId, opts = {}) {
  const baseUrl = opts.baseUrl || DEFAULT_URL;
  const intervalMs = opts.intervalMs || 3000;
  const terminal = new Set(['SUCCESS', 'FAILURE', 'CANCELED']);

  while (true) {
    const res = await fetch(`${baseUrl}/graphql`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        query: `query RunStatus($runId: ID!) { runOrError(runId: $runId) { ... on Run { runId status } } }`,
        variables: { runId },
      }),
    });

    const data = await res.json();
    const run = data?.data?.runOrError;

    if (run && terminal.has(run.status)) return run;
    await new Promise(r => setTimeout(r, intervalMs));
  }
}

/**
 * List all active Dagster run IDs for a given job.
 *
 * @param {string} jobName - Job name to filter by.
 * @param {Object} [opts={}] - Options.
 * @param {string} [opts.baseUrl] - Dagster base URL override.
 * @returns {Promise<Object[]>} Array of run objects.
 */
async function listRuns(jobName, opts = {}) {
  const baseUrl = opts.baseUrl || DEFAULT_URL;

  const res = await fetch(`${baseUrl}/graphql`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      query: `
        query Runs($filter: RunsFilter) {
          runsOrError(filter: $filter) {
            ... on Runs { results { runId status jobName startTime } }
          }
        }
      `,
      variables: { filter: { jobName } },
    }),
  });

  const data = await res.json();
  return data?.data?.runsOrError?.results || [];
}

export { triggerJob, pollRun, listRuns, encodePartitionKey };
