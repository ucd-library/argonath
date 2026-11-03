import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  listCollections, listItems, publishCollection, resolveCask, unpublishCollection,
} from '../lib/collections.js';

const API = 'https://argonath.library.ucdavis.edu/cask/api';

let dir;
let credsPath;

function writeCreds(apiUrl = API, token = 'tok-123') {
  fs.writeFileSync(credsPath, JSON.stringify({ api_url: apiUrl, token, user: 'jdoe' }));
}

function respond(body, init = {}) {
  return new Response(JSON.stringify(body), { status: 200, ...init });
}

function dirBody(names, base) {
  return { directories: names.map(name => ({ name, fullname: `${base}/${name}` })), files: [] };
}

function launchBody(runId = 'r1') {
  return { data: { launchRun: { __typename: 'LaunchRunSuccess', run: { runId, status: 'QUEUED' } } } };
}

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'argonath-'));
  credsPath = path.join(dir, 'credentials.json');
  vi.stubEnv('DIGTK_CREDENTIALS_PATH', credsPath);
  vi.stubEnv('DAGSTER_URL', '');
  vi.stubEnv('CASK_API_URL', '');
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  fs.rmSync(dir, { recursive: true, force: true });
});

describe('resolveCask', () => {
  it('falls back to local CaskFS without a login', () => {
    expect(resolveCask()).toEqual({ apiUrl: 'http://localhost:3001/cask/api', headers: {} });
  });

  it('uses the digtk login api_url and its token', () => {
    writeCreds();
    expect(resolveCask()).toEqual({ apiUrl: API, headers: { Authorization: 'Bearer tok-123' } });
  });

  it('never sends the token to another host', () => {
    writeCreds();
    expect(resolveCask({ caskUrl: 'https://example.org/cask/api' }).headers.Authorization).toBeUndefined();
  });
});

describe('listCollections', () => {
  it('lists the bronze group directory', async () => {
    writeCreds();
    const fetchMock = vi.fn(async () => respond(dirBody(['zeta', 'haeger'], '/bronze/preservica')));
    vi.stubGlobal('fetch', fetchMock);

    expect(await listCollections('preservica')).toEqual(['haeger', 'zeta']);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${API}/dir/bronze/preservica?limit=10000`);
    expect(init.headers.Authorization).toBe('Bearer tok-123');
  });

  it('returns [] for a missing group', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => respond({ error: 'not found' }, { status: 404 })));
    expect(await listCollections('nope')).toEqual([]);
  });

  it('turns a gateway login redirect into a login hint', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 302, headers: { Location: '/auth/login' } })));
    await expect(listCollections('preservica')).rejects.toThrow(/digtk auth login/);
  });
});

describe('listItems', () => {
  it('lists folders under the manifest itemsRoot', async () => {
    const latest = '/bronze/preservica/haeger/latest/tree';
    const fetchMock = vi.fn(async (url) => (url.includes('/fs/')
      ? respond({ itemsRoot: 'tree' })
      : respond(dirBody(['b-item', 'a-item'], latest))));
    vi.stubGlobal('fetch', fetchMock);

    expect(await listItems('preservica', 'haeger')).toEqual([`${latest}/a-item`, `${latest}/b-item`]);
    expect(fetchMock.mock.calls.map(c => c[0])).toEqual([
      'http://localhost:3001/cask/api/fs/bronze/preservica/haeger/manifest.json',
      `http://localhost:3001/cask/api/dir${latest}?limit=10000`,
    ]);
  });

  it('treats a missing manifest as a flat layout', async () => {
    const fetchMock = vi.fn(async (url) => (url.includes('/fs/')
      ? respond({}, { status: 404 })
      : respond(dirBody(['item'], '/bronze/g/c/latest'))));
    vi.stubGlobal('fetch', fetchMock);

    expect(await listItems('g', 'c')).toEqual(['/bronze/g/c/latest/item']);
    expect(fetchMock.mock.calls[1][0]).toBe('http://localhost:3001/cask/api/dir/bronze/g/c/latest?limit=10000');
  });
});

describe('publish and unpublish', () => {
  it('publishes with a rebuild reindex of the collection partition', async () => {
    const fetchMock = vi.fn(async () => respond(launchBody()));
    vi.stubGlobal('fetch', fetchMock);

    const run = await publishCollection('haeger', 'preservica');

    expect(run).toEqual({ runId: 'r1', url: 'http://localhost:3000/dagster/runs/r1' });
    const params = JSON.parse(fetchMock.mock.calls[0][1].body).variables.executionParams;
    expect(params.selector.jobName).toBe('showcase_reindex_collection_job');
    expect(params.runConfigData).toEqual({ ops: { fan_out_reindex_tasks: { config: { rebuild: true } } } });
    expect(params.executionMetadata.tags).toEqual([{ key: 'dagster/partition', value: 'bronze/preservica/haeger' }]);
  });

  it('unpublishes with the unpublish job', async () => {
    const fetchMock = vi.fn(async () => respond(launchBody('r2')));
    vi.stubGlobal('fetch', fetchMock);

    const run = await unpublishCollection('haeger', 'preservica');

    expect(run.runId).toBe('r2');
    const params = JSON.parse(fetchMock.mock.calls[0][1].body).variables.executionParams;
    expect(params.selector.jobName).toBe('showcase_unpublish_collection_job');
    expect(params.runConfigData).toEqual({ ops: { remove_collection_gold: { config: { purge_silver: false } } } });
    expect(params.executionMetadata.tags).toEqual([{ key: 'dagster/partition', value: 'bronze/preservica/haeger' }]);
  });

  it('passes purgeSilver to the unpublish job', async () => {
    const fetchMock = vi.fn(async () => respond(launchBody('r3')));
    vi.stubGlobal('fetch', fetchMock);

    await unpublishCollection('haeger', 'preservica', { purgeSilver: true });

    const params = JSON.parse(fetchMock.mock.calls[0][1].body).variables.executionParams;
    expect(params.runConfigData.ops.remove_collection_gold.config.purge_silver).toBe(true);
  });
});
