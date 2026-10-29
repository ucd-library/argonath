/**
 * Dagster GraphQL API client.
 * Uses fetch() to trigger jobs, check partitions and poll run status.
 * Through the auth gateway it reuses the `digtk auth login` token.
 *
 * @module dagster
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const LOCAL_URL = 'http://localhost:3000/dagster';
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
 * Read the stored `digtk auth login` credentials, if any.
 *
 * @param {Object} [opts={}] - Options.
 * @param {string} [opts.credentialsPath] - Path override (else DIGTK_CREDENTIALS_PATH, then ~/.digtk/credentials.json).
 * @returns {Object|null} Credentials with `api_url` and `token`, or null.
 */
function loadCredentials(opts = {}) {
  const file = opts.credentialsPath || process.env.DIGTK_CREDENTIALS_PATH
    || path.join(os.homedir(), '.digtk', 'credentials.json');
  try {
    const creds = JSON.parse(fs.readFileSync(file, 'utf8'));
    return creds && creds.api_url && creds.token ? creds : null;
  } catch (err) {
    return null;
  }
}

/**
 * Work out which Dagster to talk to and how to authenticate.
 * The base URL is opts.baseUrl, else DAGSTER_URL, else the digtk login's gateway, else local.
 * The login token is only sent to that login's own host.
 *
 * @param {Object} [opts={}] - Options.
 * @param {string} [opts.baseUrl] - Dagster base URL override.
 * @param {string} [opts.credentialsPath] - Credentials file override.
 * @returns {{baseUrl: string, headers: Object}} Base URL (no trailing slash) and request headers.
 */
function resolveConnection(opts = {}) {
  const creds = loadCredentials(opts);
  const gateway = creds ? creds.api_url.replace(/\/cask\/api\/?$/, '') : null;
  let baseUrl = opts.baseUrl || process.env.DAGSTER_URL || (gateway ? `${gateway}/dagster` : LOCAL_URL);
  baseUrl = baseUrl.replace(/\/+$/, '');

  const headers = { 'Content-Type': 'application/json' };
  if (creds && new URL(baseUrl).origin === new URL(creds.api_url).origin) {
    headers.Authorization = `Bearer ${creds.token}`;
  }
  return { baseUrl, headers };
}

/**
 * Run one GraphQL request against Dagster.
 * A redirect or 401/403 means the gateway wants a login (or the Dagster role).
 *
 * @param {string} query - GraphQL query or mutation.
 * @param {Object} [variables={}] - Query variables.
 * @param {Object} [opts={}] - resolveConnection() options.
 * @returns {Promise<Object>} The response's `data`.
 * @throws {Error} On HTTP, auth or GraphQL errors.
 */
async function graphql(query, variables = {}, opts = {}) {
  const { baseUrl, headers } = resolveConnection(opts);
  const res = await fetch(`${baseUrl}/graphql`, {
    method: 'POST',
    headers,
    redirect: 'manual',
    body: JSON.stringify({ query, variables }),
  });

  if ((res.status >= 300 && res.status < 400) || res.status === 401 || res.status === 403) {
    throw new Error(
      `Dagster at ${baseUrl} needs you logged in (HTTP ${res.status}). Run ` +
      '`digtk auth login --cask-env <env>`; launching runs also needs the "execute" role.'
    );
  }
  const data = await res.json().catch(() => null);
  if (data?.errors?.length) throw new Error(data.errors.map(e => e.message).join('; '));
  if (!res.ok || !data) throw new Error(`Dagster responded ${res.status} ${res.statusText}`);
  return data.data;
}

/**
 * The repository selector every mutation targets.
 *
 * @returns {Object} RepositorySelector.
 */
function repositorySelector() {
  return { repositoryLocationName: DEFAULT_LOCATION, repositoryName: DEFAULT_REPOSITORY };
}

/**
 * Trigger a Dagster job run via the launchRun GraphQL mutation.
 *
 * @param {string} jobName - Name of the Dagster job to run.
 * @param {Object} [opts={}] - Options, plus resolveConnection() options.
 * @param {Object} [opts.config] - Dagster run config to pass to the job.
 * @param {string} [opts.partitionKey] - Partition to run, e.g. "bronze/preservica/haeger".
 * @param {Object} [opts.tags] - Extra run tags, as {key: value}.
 * @returns {Promise<Object>} Run object with `runId` and `status`.
 * @throws {Error} If Dagster rejects the launch.
 */
async function triggerJob(jobName, opts = {}) {
  const tags = { ...(opts.tags || {}) };
  if (opts.partitionKey) tags['dagster/partition'] = opts.partitionKey;

  const executionParams = {
    selector: { ...repositorySelector(), jobName },
    runConfigData: opts.config || {},
    executionMetadata: {
      tags: Object.entries(tags).map(([key, value]) => ({ key, value })),
    },
  };

  const data = await graphql(`
    mutation LaunchRun($executionParams: ExecutionParams!) {
      launchRun(executionParams: $executionParams) {
        __typename
        ... on LaunchRunSuccess { run { runId status } }
        ... on Error { message }
        ... on RunConfigValidationInvalid { errors { message } }
      }
    }
  `, { executionParams }, opts);

  const result = data?.launchRun;
  if (result?.run) return result.run;
  const details = result?.errors?.map(e => e.message).join('; ') || result?.message;
  throw new Error(`${result?.__typename || 'Launch failed'}: ${details || 'no details'}`);
}

/**
 * List the partition keys a partitioned job currently has.
 *
 * @param {string} jobName - Partitioned job name.
 * @param {Object} [opts={}] - resolveConnection() options.
 * @returns {Promise<string[]>} Partition keys.
 * @throws {Error} If the job has no partition set.
 */
async function listPartitions(jobName, opts = {}) {
  const data = await graphql(`
    query Partitions($sel: RepositorySelector!) {
      repositoryOrError(repositorySelector: $sel) {
        __typename
        ... on Repository {
          partitionSets {
            pipelineName
            partitionsOrError {
              __typename
              ... on Partitions { results { name } }
              ... on PythonError { message }
            }
          }
        }
        ... on Error { message }
      }
    }
  `, { sel: repositorySelector() }, opts);

  const repo = data?.repositoryOrError;
  if (!repo?.partitionSets) throw new Error(`${repo?.__typename || 'Repository lookup failed'}: ${repo?.message || 'no details'}`);
  const set = repo.partitionSets.find(ps => ps.pipelineName === jobName);
  if (!set) throw new Error(`${jobName} has no partitions in ${DEFAULT_LOCATION}`);
  const parts = set.partitionsOrError;
  if (!parts?.results) throw new Error(`${parts?.__typename || 'Partition lookup failed'}: ${parts?.message || 'no details'}`);
  return parts.results.map(r => r.name);
}

/**
 * Wait until a partition key exists on a job, polling while a sensor registers it.
 * Dagster's API can't add op-job dynamic partitions, so a sensor must.
 *
 * @param {string} jobName - Partitioned job name.
 * @param {string} partitionKey - Key to wait for.
 * @param {Object} [opts={}] - Options, plus resolveConnection() options.
 * @param {number} [opts.intervalMs=10000] - Poll interval.
 * @param {number} [opts.timeoutMs=180000] - Give up after this long.
 * @param {Function} [opts.onWait] - Called once, before the first wait.
 * @returns {Promise<boolean>} True if it existed already, false if it appeared while waiting.
 * @throws {Error} If it never appears.
 */
async function waitForPartition(jobName, partitionKey, opts = {}) {
  const intervalMs = opts.intervalMs ?? 10000;
  const deadline = Date.now() + (opts.timeoutMs ?? 180000);
  let first = true;
  while (true) {
    if ((await listPartitions(jobName, opts)).includes(partitionKey)) return first;
    if (Date.now() >= deadline) {
      throw new Error(`partition ${partitionKey} never appeared on ${jobName}`);
    }
    if (first && opts.onWait) opts.onWait();
    first = false;
    await new Promise(r => setTimeout(r, intervalMs));
  }
}

/**
 * Poll a Dagster run until it reaches a terminal state.
 *
 * @param {string} runId - Dagster run ID.
 * @param {Object} [opts={}] - Options, plus resolveConnection() options.
 * @param {number} [opts.intervalMs=3000] - Poll interval in milliseconds.
 * @returns {Promise<Object>} Final run object with `runId` and `status`.
 */
async function pollRun(runId, opts = {}) {
  const intervalMs = opts.intervalMs || 3000;
  const terminal = new Set(['SUCCESS', 'FAILURE', 'CANCELED']);

  while (true) {
    const data = await graphql(
      'query RunStatus($runId: ID!) { runOrError(runId: $runId) { ... on Run { runId status } } }',
      { runId }, opts,
    );
    const run = data?.runOrError;
    if (run && terminal.has(run.status)) return run;
    await new Promise(r => setTimeout(r, intervalMs));
  }
}

/**
 * List runs for a given job.
 *
 * @param {string} jobName - Job name to filter by.
 * @param {Object} [opts={}] - resolveConnection() options.
 * @returns {Promise<Object[]>} Array of run objects.
 */
async function listRuns(jobName, opts = {}) {
  const data = await graphql(`
    query Runs($filter: RunsFilter) {
      runsOrError(filter: $filter) {
        ... on Runs { results { runId status jobName startTime } }
      }
    }
  `, { filter: { pipelineName: jobName } }, opts);
  return data?.runsOrError?.results || [];
}

/**
 * Link to a run in the Dagster UI.
 *
 * @param {string} runId - Dagster run ID.
 * @param {Object} [opts={}] - resolveConnection() options.
 * @returns {string} Run URL.
 */
function runUrl(runId, opts = {}) {
  return `${resolveConnection(opts).baseUrl}/runs/${runId}`;
}

export {
  triggerJob, pollRun, listRuns, encodePartitionKey, listPartitions, waitForPartition,
  resolveConnection, loadCredentials, runUrl,
};
