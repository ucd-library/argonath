#!/usr/bin/env node
/**
 * argonath — digital collections pipeline operations CLI.
 *
 * Commands:
 *   process collection <name>  — trigger a Dagster pipeline job for a collection
 *   process item <ark>         — trigger a Dagster pipeline job for a single item (requires --collection)
 *   status <run-id>            — poll a Dagster run until terminal state
 *   list collections           — list collections in a group
 *   list items <collection>    — list items in a collection
 *   publish <collection>       — promote a collection into a gold app
 *   index collection <key>     — bulk-reindex a collection into ES (no derivatives)
 *   index delete-collection <key> — delete a collection from ES
 *   index delete-item <ark>    — delete one item from ES
 *   workspace run <folder>     — run the pike tools on a workspace folder in Dagster
 */
import fs from 'node:fs';
import { Command } from 'commander';
import { triggerJob, pollRun, encodePartitionKey, listPartitions, waitForPartition, resolveConnection, runUrl } from '../lib/dagster.js';
import { listCollections, listItems, publishCollection } from '../lib/collections.js';
import { STEP_FIELDS, OPTION_FIELDS, workspaceLaunch } from '../lib/workspace.js';

const program = new Command();

program
  .name('argonath')
  .description('Digital collections pipeline operations CLI.')
  .version('0.1.0');


// ---------------------------------------------------------------------------
// argonath process
// ---------------------------------------------------------------------------

const processCmd = program.command('process').description('Trigger pipeline jobs.');

processCmd
  .command('collection <name>')
  .description('Trigger a pipeline job for an entire collection.')
  .option('--space <space>', 'App space.', 'digital-dev')
  .option('--job <job>', 'Job name: full, derivatives, ocr, metadata, alt, tag-pdf, split-detect, split-apply, rag.', 'full')
  .action(async (name, opts) => {
    const jobMap = {
      full:          'full_pipeline_job',
      derivatives:   'showcase_derivatives_job',
      ocr:           'bronze_ocr_job',
      metadata:      'metadata_job',
      alt:           'bronze_alt_text_job',
      'tag-pdf':     'bronze_tag_pdf_job',
      'split-detect': 'bronze_detect_spreads_job',
      'split-apply':  'bronze_apply_spreads_job',
      rag:           'search_index_job',
    };
    const jobName = jobMap[opts.job] || opts.job;
    try {
      const run = await triggerJob(jobName, { config: { collection: name, space: opts.space } });
      console.log(`Run started: ${run.runId}`);
    } catch (err) {
      console.error(`Error: ${err.message}`);
      process.exit(1);
    }
  });

processCmd
  .command('item <ark>')
  .description('Trigger a pipeline job for a single item by ARK.')
  .requiredOption('--collection <collection>', 'Collection key, format "[group]/[collection]".')
  .option('--job <job>', 'Job name: full, derivatives, ocr, metadata, alt, tag-pdf, split-detect, split-apply, rag.', 'full')
  .action(async (ark, opts) => {
    const jobMap = {
      full:          'full_pipeline_job',
      derivatives:   'showcase_derivatives_job',
      ocr:           'bronze_ocr_job',
      metadata:      'metadata_job',
      alt:           'bronze_alt_text_job',
      'tag-pdf':     'bronze_tag_pdf_job',
      'split-detect': 'bronze_detect_spreads_job',
      'split-apply':  'bronze_apply_spreads_job',
      rag:           'search_index_job',
    };
    const jobName = jobMap[opts.job] || opts.job;
    try {
      const run = await triggerJob(jobName, {
        partitionKeys: [{ collection: opts.collection, item: ark }],
      });
      console.log(`Run started: ${run.runId}`);
    } catch (err) {
      console.error(`Error: ${err.message}`);
      process.exit(1);
    }
  });


// ---------------------------------------------------------------------------
// argonath index
// ---------------------------------------------------------------------------

/**
 * Launch a Dagster job, print its UI link, and optionally wait for it to finish.
 *
 * @param {string} jobName - Dagster job name.
 * @param {Object} [opts={}] - Options.
 * @param {string} [opts.partitionKey] - Partition to run.
 * @param {Object} [opts.config] - Run config.
 * @param {boolean} [opts.wait] - Poll until the run finishes; exits 1 if it fails.
 * @param {string} [opts.baseUrl] - Dagster base URL override.
 * @returns {Promise<void>}
 */
async function launch(jobName, opts = {}) {
  const conn = { baseUrl: opts.baseUrl };
  const run = await triggerJob(jobName, { partitionKey: opts.partitionKey, config: opts.config, ...conn });
  console.log(`Run started: ${run.runId}`);
  console.log(runUrl(run.runId, conn));
  if (!opts.wait) return;
  const done = await pollRun(run.runId, conn);
  console.log(`Run ${done.runId}: ${done.status}`);
  if (done.status !== 'SUCCESS') process.exit(1);
}

const indexCmd = program.command('index').description('Showcase Elasticsearch index jobs (no derivatives).');

indexCmd
  .command('collection <key>')
  .description('Bulk-reindex a collection, prune orphans and refresh the collection doc. Key: bronze/<group>/<collection>.')
  .option('--rebuild', 'Rebuild each gold item record first.', false)
  .option('--no-prune', 'Keep indexed items that are no longer in bronze.')
  .option('--force-prune', 'Skip the prune safety guard.', false)
  .option('--batch-size <n>', 'Items per step / bulk request.', '200')
  .option('--wait', 'Wait for the run to finish.', false)
  .action(async (key, opts) => {
    try {
      await launch('showcase_reindex_collection_job', {
        partitionKey: key,
        wait: opts.wait,
        config: {
          ops: {
            fan_out_reindex_tasks: {
              config: {
                rebuild: opts.rebuild,
                prune: opts.prune,
                force_prune: opts.forcePrune,
                batch_size: parseInt(opts.batchSize),
              },
            },
          },
        },
      });
    } catch (err) {
      console.error(`Error: ${err.message}`);
      process.exit(1);
    }
  });

indexCmd
  .command('delete-collection <key>')
  .description('Delete a collection, its items and their pages from ES. Key: bronze/<group>/<collection>.')
  .option('--wait', 'Wait for the run to finish.', false)
  .action(async (key, opts) => {
    try {
      await launch('showcase_delete_collection_index_job', { partitionKey: key, wait: opts.wait });
    } catch (err) {
      console.error(`Error: ${err.message}`);
      process.exit(1);
    }
  });

indexCmd
  .command('delete-item <ark>')
  .description('Delete one item and its pages from ES, then refresh its collection doc.')
  .option('--collection <key>', 'Collection key to refresh if the item is gone from cask.', '')
  .option('--wait', 'Wait for the run to finish.', false)
  .action(async (ark, opts) => {
    try {
      await launch('showcase_delete_item_index_job', {
        wait: opts.wait,
        config: { ops: { delete_item_index: { config: { ark, collection: opts.collection } } } },
      });
    } catch (err) {
      console.error(`Error: ${err.message}`);
      process.exit(1);
    }
  });


// ---------------------------------------------------------------------------
// argonath workspace
// ---------------------------------------------------------------------------

const workspaceCmd = program.command('workspace').description('Workspace folder jobs (scans staged with `digtk workspace add`).');

workspaceCmd
  .command('run <folder>')
  .description('Run the pike tools on a workspace folder in Dagster (workspace_process_job). '
    + 'Folder: cask:/workspace/<user>/<folder>.')
  .option('--steps <steps>', `Steps to run (comma-separated): ${Object.keys(STEP_FIELDS).join(', ')}, or all. `
    + `Also turns on options listed here: ${Object.keys(OPTION_FIELDS).join(', ')}. Required: every step defaults to off.`)
  .option('--set <key=value>', 'Set any fan_out_workspace option, e.g. tables=true (repeatable).', (v, all) => all.concat(v), [])
  .option('--config <file>', 'JSON file with fan_out_workspace options (--steps and --set win).')
  .option('--wait', 'Wait for the run to finish.', false)
  .option('--dry-run', 'Print the partition, whether it exists yet, and the run config without launching.', false)
  .option('--dagster-url <url>', 'Dagster URL (default: DAGSTER_URL, else your digtk login\'s gateway).')
  .action(async (folder, opts) => {
    try {
      const config = opts.config ? JSON.parse(fs.readFileSync(opts.config, 'utf8')) : undefined;
      const plan = workspaceLaunch(folder, { config, steps: opts.steps, set: opts.set });
      const conn = { baseUrl: opts.dagsterUrl };
      if (opts.dryRun) {
        const partitionExists = (await listPartitions(plan.jobName, conn)).includes(plan.partitionKey);
        console.log(JSON.stringify({ dagster: resolveConnection(conn).baseUrl, partitionExists, ...plan }, null, 2));
        return;
      }
      // sensor registers new folders (~2 min)
      await waitForPartition(plan.jobName, plan.partitionKey, {
        ...conn,
        onWait: () => console.log(`Waiting for workspace_partition_sync_sensor to register ${plan.partitionKey}...`),
      }).catch((err) => {
        throw new Error(`${err.message}. Check workspace_partition_sync_sensor is on and the folder has a source/ directory.`);
      });
      console.log(`Partition ${plan.partitionKey}`);
      await launch(plan.jobName, {
        partitionKey: plan.partitionKey, config: plan.runConfig, wait: opts.wait, ...conn,
      });
    } catch (err) {
      console.error(`Error: ${err.message}`);
      process.exit(1);
    }
  });


// ---------------------------------------------------------------------------
// argonath status
// ---------------------------------------------------------------------------

program
  .command('status <run-id>')
  .description('Poll a Dagster run until it reaches a terminal state.')
  .option('--interval <ms>', 'Poll interval in milliseconds.', '3000')
  .action(async (runId, opts) => {
    console.log(`Polling run ${runId}...`);
    try {
      const run = await pollRun(runId, { intervalMs: parseInt(opts.interval) });
      console.log(`Run ${run.runId}: ${run.status}`);
      if (run.status === 'FAILURE') process.exit(1);
    } catch (err) {
      console.error(`Error: ${err.message}`);
      process.exit(1);
    }
  });


// ---------------------------------------------------------------------------
// argonath list
// ---------------------------------------------------------------------------

const listCmd = program.command('list').description('List collections or items.');

listCmd
  .command('collections')
  .description('List all bronze collections in a group.')
  .requiredOption('--group <group>', 'Group namespace to list within.')
  .option('--format <format>', 'Output format: table or json.', 'table')
  .action(async (opts) => {
    try {
      const collections = await listCollections(opts.group);
      if (opts.format === 'json') {
        console.log(JSON.stringify(collections, null, 2));
      } else {
        collections.forEach(c => console.log(c));
      }
    } catch (err) {
      console.error(`Error: ${err.message}`);
      process.exit(1);
    }
  });

listCmd
  .command('items <collection>')
  .description("List all items in a bronze collection's latest (or most recent) snapshot.")
  .requiredOption('--group <group>', 'Group namespace the collection belongs to.')
  .option('--format <format>', 'Output format: table or json.', 'table')
  .action(async (collection, opts) => {
    try {
      const items = await listItems(opts.group, collection);
      if (opts.format === 'json') {
        console.log(JSON.stringify(items, null, 2));
      } else {
        items.forEach(i => console.log(i));
      }
    } catch (err) {
      console.error(`Error: ${err.message}`);
      process.exit(1);
    }
  });


// ---------------------------------------------------------------------------
// argonath publish
// ---------------------------------------------------------------------------

program
  .command('publish <collection>')
  .description('Promote a bronze/silver collection into a gold app.')
  .requiredOption('--group <group>', 'Group namespace the collection belongs to.')
  .requiredOption('--to <app>',      'Destination gold app (e.g. digital-dev, digital-prod, arda).')
  .action(async (collection, opts) => {
    try {
      const result = await publishCollection(collection, opts.group, opts.to);
      console.log(`Published ${collection}: ${result.itemCount} items promoted from ${opts.group}/${collection} → gold/${opts.to}`);
    } catch (err) {
      console.error(`Error: ${err.message}`);
      process.exit(1);
    }
  });


program.parse(process.argv);
