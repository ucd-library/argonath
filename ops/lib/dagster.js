/**
 * Dagster REST API client.
 * Uses fetch() to trigger jobs, poll run status, and list collections/items.
 *
 * @module dagster
 */

const DEFAULT_URL = process.env.DAGSTER_URL || 'http://localhost:3000/dagster';
const DEFAULT_LOCATION = process.env.DAGSTER_LOCATION || 'defs.py';
const DEFAULT_REPOSITORY = process.env.DAGSTER_REPOSITORY || '__repository__';

/**
 * Encode a (collection, item) pair as a Dagster MultiPartitionKey string.
 * Dagster joins dimension keys sorted by dimension name with "|".
 *
 * @param {string} collection - Collection partition key (format "[group]/[collection]").
 * @param {string} item - Item partition key (the item's ARK).
 * @returns {string} Encoded multi-partition key.
 */
function encodePartitionKey(collection, item) {
  return `${collection}|${item}`;
}

/**
 * Trigger a Dagster job run via the launchRun GraphQL mutation.
 *
 * @param {string} jobName - Name of the Dagster job to run.
 * @param {Object} [opts={}] - Options.
 * @param {string} [opts.baseUrl] - Dagster base URL override.
 * @param {Object} [opts.config] - Dagster run config to pass to the job.
 * @param {string} [opts.partitionKey] - Partition to run, e.g. "bronze/preservica/haeger".
 * @param {Object} [opts.tags] - Extra run tags, as {key: value}.
 * @returns {Promise<Object>} Run object with `runId` and `status`.
 * @throws {Error} If Dagster rejects the launch.
 */
async function triggerJob(jobName, opts = {}) {
  const baseUrl = opts.baseUrl || DEFAULT_URL;
  const tags = { ...(opts.tags || {}) };
  if (opts.partitionKey) tags['dagster/partition'] = opts.partitionKey;

  const executionParams = {
    selector: {
      repositoryLocationName: DEFAULT_LOCATION,
      repositoryName: DEFAULT_REPOSITORY,
      jobName,
    },
    runConfigData: opts.config || {},
    executionMetadata: {
      tags: Object.entries(tags).map(([key, value]) => ({ key, value })),
    },
  };

  const res = await fetch(`${baseUrl}/graphql`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      query: `
        mutation LaunchRun($executionParams: ExecutionParams!) {
          launchRun(executionParams: $executionParams) {
            __typename
            ... on LaunchRunSuccess { run { runId status } }
            ... on Error { message }
            ... on RunConfigValidationInvalid { errors { message } }
          }
        }
      `,
      variables: { executionParams },
    }),
  });

  if (!res.ok) throw new Error(`Dagster responded ${res.status} ${res.statusText}`);
  const data = await res.json();
  if (data.errors?.length) throw new Error(data.errors.map(e => e.message).join('; '));

  const result = data?.data?.launchRun;
  if (result?.run) return result.run;
  const details = result?.errors?.map(e => e.message).join('; ') || result?.message;
  throw new Error(`${result?.__typename || 'Launch failed'}: ${details || 'no details'}`);
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
