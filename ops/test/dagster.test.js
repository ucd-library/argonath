import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  listPartitions, listRuns, resolveConnection, runUrl, triggerJob, waitForPartition,
} from '../lib/dagster.js';

let dir;
let credsPath;

function writeCreds(apiUrl, token = 'tok-123') {
  fs.writeFileSync(credsPath, JSON.stringify({ api_url: apiUrl, token, user: 'jdoe' }));
}

function respond(body, init = {}) {
  return new Response(JSON.stringify(body), { status: 200, ...init });
}

function partitionsBody(keys) {
  return { data: { repositoryOrError: { __typename: 'Repository', partitionSets: [
    { pipelineName: 'bronze_ocr_job', partitionsOrError: { __typename: 'Partitions', results: [] } },
    { pipelineName: 'workspace_process_job', partitionsOrError: {
      __typename: 'Partitions', results: keys.map(name => ({ name })) } },
  ] } } };
}

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'argonath-'));
  credsPath = path.join(dir, 'credentials.json');
  vi.stubEnv('DIGTK_CREDENTIALS_PATH', credsPath);
  vi.stubEnv('DAGSTER_URL', '');
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  fs.rmSync(dir, { recursive: true, force: true });
});

describe('resolveConnection', () => {
  it('falls back to local Dagster without a login', () => {
    expect(resolveConnection()).toEqual({
      baseUrl: 'http://localhost:3000/dagster',
      headers: { 'Content-Type': 'application/json' },
    });
  });

  it('uses the digtk login gateway and its token', () => {
    writeCreds('https://argonath.library.ucdavis.edu/cask/api');
    const conn = resolveConnection();
    expect(conn.baseUrl).toBe('https://argonath.library.ucdavis.edu/dagster');
    expect(conn.headers.Authorization).toBe('Bearer tok-123');
  });

  it('prefers DAGSTER_URL, then opts.baseUrl', () => {
    writeCreds('https://argonath.library.ucdavis.edu/cask/api');
    vi.stubEnv('DAGSTER_URL', 'https://argonath.library.ucdavis.edu/dagster/');
    expect(resolveConnection().baseUrl).toBe('https://argonath.library.ucdavis.edu/dagster');
    expect(resolveConnection({ baseUrl: 'http://other:3000/dagster' }).baseUrl).toBe('http://other:3000/dagster');
  });

  it('never sends the token to another host', () => {
    writeCreds('https://argonath.library.ucdavis.edu/cask/api');
    const conn = resolveConnection({ baseUrl: 'https://example.org/dagster' });
    expect(conn.headers.Authorization).toBeUndefined();
  });

  it('ignores an unreadable credentials file', () => {
    fs.writeFileSync(credsPath, 'not json');
    expect(resolveConnection().headers.Authorization).toBeUndefined();
  });
});

describe('graphql calls', () => {
  it('launches with the partition tag and auth header', async () => {
    writeCreds('https://argonath.library.ucdavis.edu/cask/api');
    const fetchMock = vi.fn(async () => respond({
      data: { launchRun: { __typename: 'LaunchRunSuccess', run: { runId: 'r1', status: 'QUEUED' } } },
    }));
    vi.stubGlobal('fetch', fetchMock);

    const run = await triggerJob('workspace_process_job', {
      partitionKey: 'workspace/jdoe/box-12', config: { ops: {} },
    });

    expect(run).toEqual({ runId: 'r1', status: 'QUEUED' });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('https://argonath.library.ucdavis.edu/dagster/graphql');
    expect(init.redirect).toBe('manual');
    expect(init.headers.Authorization).toBe('Bearer tok-123');
    const params = JSON.parse(init.body).variables.executionParams;
    expect(params.selector).toEqual({
      repositoryLocationName: 'defs.py', repositoryName: '__repository__', jobName: 'workspace_process_job',
    });
    expect(params.executionMetadata.tags).toEqual([{ key: 'dagster/partition', value: 'workspace/jdoe/box-12' }]);
  });

  it('turns a gateway login redirect into a login hint', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', {
      status: 302, headers: { Location: '/auth/login' },
    })));
    await expect(triggerJob('workspace_process_job')).rejects.toThrow(/digtk auth login/);
  });

  it('reports launch errors', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => respond({
      data: { launchRun: { __typename: 'RunConfigValidationInvalid', errors: [{ message: 'bad field' }] } },
    })));
    await expect(triggerJob('workspace_process_job')).rejects.toThrow('RunConfigValidationInvalid: bad field');
  });

  it('lists a job\'s partitions', async () => {
    const fetchMock = vi.fn(async () => respond(partitionsBody(['workspace/jdoe/box-12'])));
    vi.stubGlobal('fetch', fetchMock);

    expect(await listPartitions('workspace_process_job')).toEqual(['workspace/jdoe/box-12']);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).variables).toEqual({
      sel: { repositoryLocationName: 'defs.py', repositoryName: '__repository__' },
    });
    await expect(listPartitions('no_such_job')).rejects.toThrow('no_such_job has no partitions');
  });

  it('returns at once when the partition exists', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => respond(partitionsBody(['workspace/jdoe/box-12']))));
    const onWait = vi.fn();
    expect(await waitForPartition('workspace_process_job', 'workspace/jdoe/box-12', { onWait })).toBe(true);
    expect(onWait).not.toHaveBeenCalled();
  });

  it('waits for the sensor to register a new partition', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(respond(partitionsBody([])))
      .mockResolvedValueOnce(respond(partitionsBody([])))
      .mockResolvedValueOnce(respond(partitionsBody(['workspace/jdoe/box-12'])));
    vi.stubGlobal('fetch', fetchMock);
    const onWait = vi.fn();

    const existed = await waitForPartition('workspace_process_job', 'workspace/jdoe/box-12', {
      intervalMs: 1, timeoutMs: 1000, onWait,
    });

    expect(existed).toBe(false);
    expect(onWait).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('gives up when the partition never appears', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => respond(partitionsBody([]))));
    await expect(waitForPartition('workspace_process_job', 'workspace/jdoe/box-12', {
      intervalMs: 1, timeoutMs: 5,
    })).rejects.toThrow('never appeared');
  });

  it('surfaces GraphQL errors from a non-200 response', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => respond(
      { data: null, errors: [{ message: "Field 'x' is not defined" }] }, { status: 500 },
    )));
    await expect(triggerJob('workspace_process_job')).rejects.toThrow("Field 'x' is not defined");
  });

  it('filters runs by job name', async () => {
    const fetchMock = vi.fn(async () => respond({ data: { runsOrError: { results: [{ runId: 'r1' }] } } }));
    vi.stubGlobal('fetch', fetchMock);
    expect(await listRuns('workspace_process_job')).toEqual([{ runId: 'r1' }]);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).variables).toEqual({ filter: { pipelineName: 'workspace_process_job' } });
  });

  it('links to the run in the UI', () => {
    writeCreds('https://argonath.library.ucdavis.edu/cask/api');
    expect(runUrl('r1')).toBe('https://argonath.library.ucdavis.edu/dagster/runs/r1');
  });
});
