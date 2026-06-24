/**
 * Collection and item listing helpers.
 * Reads collection metadata from CaskFS via the configured API.
 *
 * @module collections
 */

const DEFAULT_CASK_URL = process.env.CASK_ROOT || 'http://localhost:3001';

/**
 * List all collection names in a given app space.
 *
 * @param {string} space - App space name (e.g. 'arda', 'digital-dev', 'digital-prod').
 * @param {Object} [opts={}] - Options.
 * @param {string} [opts.caskUrl] - CaskFS base URL override.
 * @returns {Promise<string[]>} Array of collection names.
 */
async function listCollections(space, opts = {}) {
  throw new Error('phase 0.8');
}

/**
 * List all item ARKs in a collection.
 *
 * @param {string} space - App space name.
 * @param {string} collection - Collection name.
 * @param {Object} [opts={}] - Options.
 * @param {string} [opts.caskUrl] - CaskFS base URL override.
 * @returns {Promise<string[]>} Array of ARK strings.
 */
async function listItems(space, collection, opts = {}) {
  throw new Error('phase 0.8');
}

/**
 * Copy a collection from one space to another via CaskFS.
 * CaskFS SHA-256 content addressing deduplicates the underlying binaries.
 *
 * @param {string} collection - Collection name.
 * @param {string} fromSpace - Source app space.
 * @param {string} toSpace - Destination app space.
 * @param {Object} [opts={}] - Options.
 * @param {string} [opts.caskUrl] - CaskFS base URL override.
 * @returns {Promise<Object>} Result with item count copied.
 */
async function publishCollection(collection, fromSpace, toSpace, opts = {}) {
  throw new Error('phase 0.8');
}

export { listCollections, listItems, publishCollection };
