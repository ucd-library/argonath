/**
 * Workspace folder runs: turn CLI input into a workspace_process_job launch.
 * A workspace folder is cask:/workspace/<user>/<folder> with a source/ dir.
 *
 * @module workspace
 */

const PARTITIONS_DEF = 'workspace';
const JOB_NAME = 'workspace_process_job';
const FAN_OUT_OP = 'fan_out_workspace';

// option names --steps also accepts
const OPTION_FIELDS = {
  vision: 'vision',
  'reuse-ocr': 'reuse_ocr',
  'embed-fonts': 'embed_fonts',
  validate: 'validate',
  'want-description': 'want_description',
  'want-long': 'want_long',
  tables: 'tables',
  strict: 'strict',
  paragraphs: 'paragraphs',
  correct: 'correct',
  'skip-existing': 'skip_existing',
  'ocr-text-pages': 'ocr_text_pages',
};

// `digtk workspace run` step -> config field
const STEP_FIELDS = {
  'split-detect': 'split_detect',
  'split-apply': 'split_apply',
  ocr: 'ocr',
  'ocr-correct': 'ocr_correct',
  'alt-text': 'alt_text',
  figures: 'figures',
  'pdf-build': 'pdf_build',
  'tag-pdf': 'tag_pdf',
};

/**
 * Parse a workspace folder target into its user, folder and partition key.
 * Accepts cask:/workspace/<user>/<folder>, /workspace/<user>/<folder> or <user>/<folder>.
 *
 * @param {string} target - The folder target.
 * @returns {{username: string, folder: string, partitionKey: string}} Parsed target.
 * @throws {Error} If the target isn't a workspace folder.
 */
function parseTarget(target) {
  const parts = String(target || '')
    .replace(/^cask:/, '')
    .split('/')
    .filter(Boolean);
  if (parts[0] === 'workspace') parts.shift();
  if (parts.length !== 2) {
    throw new Error(
      `expected a workspace folder like cask:/workspace/<user>/<folder>, got "${target}"`
    );
  }
  const [username, folder] = parts;
  return { username, folder, partitionKey: `${PARTITIONS_DEF}/${username}/${folder}` };
}

/**
 * Parse one --set value: JSON when it parses (true, 60, "x"), else a plain string.
 *
 * @param {string} pair - "key=value".
 * @returns {[string, *]} Key and value.
 * @throws {Error} If there's no "=".
 */
function parseSet(pair) {
  const i = pair.indexOf('=');
  if (i < 1) throw new Error(`--set expects key=value, got "${pair}"`);
  const raw = pair.slice(i + 1);
  try {
    return [pair.slice(0, i), JSON.parse(raw)];
  } catch (err) {
    return [pair.slice(0, i), raw];
  }
}

/**
 * Build the fan_out_workspace config from a config file, --steps and --set.
 * Later layers win; --steps ticks only the listed steps and turns on listed options.
 * "all" in --steps means every step.
 *
 * @param {Object} [opts={}] - Options.
 * @param {Object} [opts.config] - Parsed --config file (the fan_out_workspace config).
 * @param {string} [opts.steps] - Comma-separated step and option names.
 * @param {string[]} [opts.set] - "key=value" overrides.
 * @returns {Object} The op config; {} keeps the job's defaults.
 * @throws {Error} On an unknown name.
 */
function buildFanOutConfig(opts = {}) {
  const config = { ...(opts.config || {}) };
  if (opts.steps) {
    let names = opts.steps.split(',').map(s => s.trim()).filter(Boolean);
    if (names.includes('all')) {
      names = [...Object.keys(STEP_FIELDS), ...names.filter(n => n !== 'all')];
    }
    const unknown = names.filter(n => !STEP_FIELDS[n] && !OPTION_FIELDS[n]);
    if (unknown.length) {
      throw new Error(
        `unknown step(s) ${unknown.join(', ')}; use ${Object.keys(STEP_FIELDS).join(', ')}, `
        + `all, or options ${Object.keys(OPTION_FIELDS).join(', ')}`
      );
    }
    for (const [step, field] of Object.entries(STEP_FIELDS)) {
      config[field] = names.includes(step);
    }
    for (const [name, field] of Object.entries(OPTION_FIELDS)) {
      if (names.includes(name)) config[field] = true;
    }
  }
  for (const pair of opts.set || []) {
    const [key, value] = parseSet(pair);
    config[key] = value;
  }
  return config;
}

/**
 * The partition key and run config for one workspace folder launch.
 *
 * @param {string} target - Workspace folder target.
 * @param {Object} [opts={}] - buildFanOutConfig() options.
 * @returns {{jobName: string, partitionKey: string, runConfig: Object}} Launch plan.
 * @throws {Error} When no step is selected.
 */
function workspaceLaunch(target, opts = {}) {
  const { partitionKey } = parseTarget(target);
  const fanOut = buildFanOutConfig(opts);
  // every step defaults to off
  if (!Object.values(STEP_FIELDS).some(field => fanOut[field] === true)) {
    throw new Error('no steps selected: pass --steps, e.g. --steps all,vision,reuse-ocr,embed-fonts,validate');
  }
  const runConfig = Object.keys(fanOut).length
    ? { ops: { [FAN_OUT_OP]: { config: fanOut } } }
    : {};
  return { jobName: JOB_NAME, partitionKey, runConfig };
}

export { STEP_FIELDS, OPTION_FIELDS, parseTarget, parseSet, buildFanOutConfig, workspaceLaunch };
