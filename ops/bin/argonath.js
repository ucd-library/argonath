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
 */
import { Command } from 'commander';
import { triggerJob, pollRun, encodePartitionKey } from '../lib/dagster.js';
import { listCollections, listItems, publishCollection } from '../lib/collections.js';

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
  .option('--job <job>', 'Job name: full, derivatives, ocr, metadata, rag.', 'full')
  .action(async (name, opts) => {
    const jobMap = {
      full:        'full_pipeline_job',
      derivatives: 'derivatives_job',
      ocr:         'ocr_job',
      metadata:    'metadata_job',
      rag:         'search_index_job',
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
  .option('--job <job>', 'Job name: full, derivatives, ocr, metadata, rag.', 'full')
  .action(async (ark, opts) => {
    const jobMap = {
      full:        'full_pipeline_job',
      derivatives: 'derivatives_job',
      ocr:         'ocr_job',
      metadata:    'metadata_job',
      rag:         'search_index_job',
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
