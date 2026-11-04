/**
 * Collection listing, publish and unpublish helpers.
 * Lists bronze collections/items over the CaskFS API and launches the
 * Dagster jobs that publish or unpublish a collection's gold records.
 *
 * @module collections
 */
import { loadCredentials, runUrl, triggerJob } from './dagster.js';

const LOCAL_CASK_API = 'http://localhost:3001/cask/api';
const LS_PAGE_SIZE = 10000;

const PUBLISH_JOB = 'showcase_reindex_collection_job';
const UNPUBLISH_JOB = 'showcase_unpublish_collection_job';

/**
 * Work out which CaskFS API to talk to and how to authenticate.
 * The URL is opts.caskUrl, else CASK_API_URL, else the digtk login's api_url, else local.
 * The login token is only sent to that login's own host.
 *
 * @param {Object} [opts={}] - Options.
 * @param {string} [opts.caskUrl] - CaskFS API URL override (ends in /cask/api).
 * @param {string} [opts.credentialsPath] - Credentials file override.
 * @returns {{apiUrl: string, headers: Object}} API URL (no trailing slash) and request headers.
 */
function resolveCask(opts = {}) {
  const creds = loadCredentials(opts);
  let apiUrl = opts.caskUrl || process.env.CASK_API_URL || (creds ? creds.api_url : LOCAL_CASK_API);
  apiUrl = apiUrl.replace(/\/+$/, '');

  const headers = {};
  if (creds && new URL(apiUrl).origin === new URL(creds.api_url).origin) {
    headers.Authorization = `Bearer ${creds.token}`;
  }
  return { apiUrl, headers };
}

/**
 * GET a CaskFS API path and parse its JSON body.
 *
 * @param {string} apiPath - Path under the API, e.g. '/dir/bronze/preservica'.
 * @param {Object} [opts={}] - resolveCask() options.
 * @returns {Promise<Object|null>} Parsed body, or null on a 404.
 * @throws {Error} On auth or HTTP errors.
 */
async function caskFetch(apiPath, opts = {}) {
  const { apiUrl, headers } = resolveCask(opts);
  const res = await fetch(`${apiUrl}${apiPath}`, { headers, redirect: 'manual' });

  if ((res.status >= 300 && res.status < 400) || res.status === 401 || res.status === 403) {
    throw new Error(
      `CaskFS at ${apiUrl} needs you logged in (HTTP ${res.status}). Run \`digtk auth login --cask-env <env>\`.`
    );
  }
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`CaskFS responded ${res.status} ${res.statusText} for ${apiPath}`);
  return res.json();
}

/**
 * Names of the subdirectories of a CaskFS directory.
 *
 * @param {string} dirPath - CaskFS-absolute directory path.
 * @param {Object} [opts={}] - resolveCask() options.
 * @returns {Promise<Object[]>} Directory entries with `name` and `fullname`; [] if missing.
 */
async function listDirectories(dirPath, opts = {}) {
  const body = await caskFetch(`/dir${dirPath}?limit=${LS_PAGE_SIZE}`, opts);
  return body?.directories || [];
}

/**
 * List all bronze collection names in a given group.
 *
 * @param {string} group - Group namespace (e.g. 'preservica', 'website-harvests').
 * @param {Object} [opts={}] - resolveCask() options.
 * @returns {Promise<string[]>} Collection names, sorted.
 */
async function listCollections(group, opts = {}) {
  const dirs = await listDirectories(`/bronze/${group}`, opts);
  return dirs.map(d => d.name).sort();
}

/**
 * List the item folders in a bronze collection's latest snapshot.
 * Items are the folders under the collection manifest's `itemsRoot` (flat if unset),
 * matching digtk.models.collection.list_item_folders().
 *
 * @param {string} group - Group namespace the collection belongs to.
 * @param {string} collection - Collection name.
 * @param {Object} [opts={}] - resolveCask() options.
 * @returns {Promise<string[]>} Item folder paths, e.g. '/bronze/preservica/haeger/latest/tree/quarter-mile', sorted.
 */
async function listItems(group, collection, opts = {}) {
  const root = `/bronze/${group}/${collection}`;
  const manifest = await caskFetch(`/fs${root}/manifest.json`, opts);
  const itemsRoot = manifest?.itemsRoot ? `/${manifest.itemsRoot}` : '';
  const dirs = await listDirectories(`${root}/latest${itemsRoot}`, opts);
  return dirs.map(d => d.fullname).sort();
}

/**
 * The Dagster partition key for a bronze collection.
 *
 * @param {string} group - Group namespace.
 * @param {string} collection - Collection name.
 * @returns {string} e.g. 'bronze/preservica/haeger'.
 */
function collectionKey(group, collection) {
  return `bronze/${group}/${collection}`;
}

/**
 * Publish a collection into the deployment's gold project.
 * Launches the reindex job with rebuild on: build-item (records plus promoted
 * binaries), ES upsert, prune, then the collection record. Items with no silver
 * derivatives (e.g. after a purge) are queued for full item runs instead.
 *
 * @param {string} collection - Collection name.
 * @param {string} group - Group namespace the collection belongs to.
 * @param {Object} [opts={}] - triggerJob() options (e.g. baseUrl).
 * @returns {Promise<{runId: string, url: string}>} Launched run.
 */
async function publishCollection(collection, group, opts = {}) {
  const run = await triggerJob(PUBLISH_JOB, {
    ...opts,
    partitionKey: collectionKey(group, collection),
    config: { ops: { fan_out_reindex_tasks: { config: { rebuild: true } } } },
  });
  return { runId: run.runId, url: runUrl(run.runId, opts) };
}

/**
 * Unpublish a collection from the deployment's gold project.
 * Deletes its ES documents, then its gold folder, and with purgeSilver its
 * silver website derivatives. Bronze and the collection registry are left alone.
 *
 * @param {string} collection - Collection name.
 * @param {string} group - Group namespace the collection belongs to.
 * @param {Object} [opts={}] - Options, plus triggerJob() options (e.g. baseUrl).
 * @param {boolean} [opts.purgeSilver=false] - Also delete silver website derivatives.
 * @returns {Promise<{runId: string, url: string}>} Launched run.
 */
async function unpublishCollection(collection, group, opts = {}) {
  const { purgeSilver = false, ...jobOpts } = opts;
  const run = await triggerJob(UNPUBLISH_JOB, {
    ...jobOpts,
    partitionKey: collectionKey(group, collection),
    config: { ops: { remove_collection_gold: { config: { purge_silver: purgeSilver } } } },
  });
  return { runId: run.runId, url: runUrl(run.runId, jobOpts) };
}

export { listCollections, listItems, publishCollection, unpublishCollection, resolveCask };
