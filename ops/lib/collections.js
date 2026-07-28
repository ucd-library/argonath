/**
 * Collection and item listing helpers.
 * Reads collection metadata from CaskFS via the configured API.
 *
 * @module collections
 */

const DEFAULT_CASK_URL = process.env.CASK_ROOT || 'http://localhost:3001';

/**
 * List all bronze collection names in a given group.
 *
 * @param {string} group - Group namespace (e.g. 'digital-collections', 'website-harvests').
 * @param {Object} [opts={}] - Options.
 * @param {string} [opts.caskUrl] - CaskFS base URL override.
 * @returns {Promise<string[]>} Array of collection names.
 */
async function listCollections(group, opts = {}) {
  throw new Error('phase 0.8');
}

/**
 * List all item ARKs in a bronze collection's latest (or most recent) snapshot.
 *
 * @param {string} group - Group namespace the collection belongs to.
 * @param {string} collection - Collection name.
 * @param {Object} [opts={}] - Options.
 * @param {string} [opts.caskUrl] - CaskFS base URL override.
 * @returns {Promise<string[]>} Array of ARK strings.
 */
async function listItems(group, collection, opts = {}) {
  throw new Error('phase 0.8');
}

/**
 * Promote a bronze/silver collection into a gold app via CaskFS.
 * Gold entries reference bronze/silver content rather than copying it, so this
 * writes thin sidecars (manifest.json + item.jsonld.json per item), not binaries.
 *
 * @param {string} collection - Collection name.
 * @param {string} group - Group namespace the collection belongs to.
 * @param {string} toApp - Destination gold app (e.g. 'digital-dev', 'digital-prod', 'arda').
 * @param {Object} [opts={}] - Options.
 * @param {string} [opts.caskUrl] - CaskFS base URL override.
 * @returns {Promise<Object>} Result with item count promoted.
 */
async function publishCollection(collection, group, toApp, opts = {}) {
  throw new Error('phase 0.8');
}

export { listCollections, listItems, publishCollection };
