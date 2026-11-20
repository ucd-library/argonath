import { afterEach, describe, expect, it, vi } from 'vitest';
import { PreservicaWriter, buildAssetXip, oaiDc, zipAssetPackage } from '../lib/preservica.js';

const BASE = 'https://preservica.test';
const VERSION_XML = '<VersionDetails><CurrentVersion>7.5.1</CurrentVersion></VersionDetails>';

/**
 * Route fetch calls to handlers keyed by "METHOD path-prefix"; records every call.
 *
 * @param {Object} routes - {"GET /api/...": (url, init) => Response}.
 * @returns {Array<Object>} Recorded calls, [{method, path, init}].
 */
function stubFetch(routes) {
  const calls = [];
  vi.stubGlobal('fetch', vi.fn(async (url, init = {}) => {
    const method = init.method || 'GET';
    const path = url.replace(BASE, '');
    calls.push({ method, path, init });
    const key = Object.keys(routes)
      .filter(k => k.startsWith(`${method} `) && path.startsWith(k.slice(method.length + 1)))
      .sort((a, b) => b.length - a.length)[0];
    if (!key) throw new Error(`Unrouted ${method} ${path}`);
    return routes[key](url, init);
  }));
  return calls;
}

function xml(body, status = 200) {
  return new Response(status === 204 ? null : body, { status });
}

function writer(opts = {}) {
  return new PreservicaWriter({
    baseUrl: BASE, tenant: 'STARTER1', username: 'op', password: 'pw',
    managerUsername: 'mgr', managerPassword: 'mpw', ...opts,
  });
}

const login = () => new Response(JSON.stringify({ token: 'tok-op' }), { status: 200 });

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('login', () => {
  it('defaults the base URL from the tenant', () => {
    const w = new PreservicaWriter({ tenant: 'starter1', username: 'op', password: 'pw' });
    expect(w.baseUrl).toBe('https://starter1.preservica.com');
  });

  it('posts form credentials without the tenant and keeps the operator token', async () => {
    const calls = stubFetch({ 'POST /api/accesstoken/login': login });
    const w = writer();
    expect(await w.login()).toBe('tok-op');
    expect(w.token).toBe('tok-op');
    const body = calls[0].init.body;
    expect(body.get('username')).toBe('op');
    expect(body.has('tenant')).toBe(false);
  });

  it('throws on a failed login', async () => {
    stubFetch({ 'POST /api/accesstoken/login': () => new Response('{"message":"bad"}', { status: 401 }) });
    await expect(writer().login()).rejects.toThrow(/login failed for op/);
  });

  it('re-logs in once on a 401', async () => {
    let first = true;
    stubFetch({
      'POST /api/accesstoken/login': login,
      'GET /api/entity/versiondetails/version': () => {
        if (first) { first = false; return xml('', 401); }
        return xml(VERSION_XML);
      },
    });
    const ns = await writer().namespaces();
    expect(ns.xip).toBe('http://preservica.com/XIP/v7.5');
  });
});

describe('createFolder and identifiers', () => {
  it('creates a folder under a parent with the version namespace', async () => {
    const calls = stubFetch({
      'POST /api/accesstoken/login': login,
      'GET /api/entity/versiondetails/version': () => xml(VERSION_XML),
      'POST /api/entity/structural-objects': (url, init) => {
        const ref = init.body.match(/<Ref>([^<]+)<\/Ref>/)[1];
        return xml(`<EntityResponse><xip:StructuralObject xmlns:xip="x"><xip:Ref>${ref}</xip:Ref></xip:StructuralObject></EntityResponse>`);
      },
    });
    const ref = await writer().createFolder('e2e-run', { parent: 'parent-ref' });
    const post = calls.find(c => c.path === '/api/entity/structural-objects');
    expect(post.init.body).toContain('xmlns="http://preservica.com/XIP/v7.5"');
    expect(post.init.body).toContain('<Parent>parent-ref</Parent>');
    expect(post.init.headers['Preservica-Access-Token']).toBe('tok-op');
    expect(ref).toMatch(/^[0-9a-f-]{36}$/);
  });

  it('adds an identifier and deletes only matching ones', async () => {
    const calls = stubFetch({
      'POST /api/accesstoken/login': login,
      'GET /api/entity/versiondetails/version': () => xml(VERSION_XML),
      'POST /api/entity/structural-objects/so-1/identifiers': () => xml('<IdentifiersResponse><Identifier><ApiId>a1</ApiId></Identifier></IdentifiersResponse>'),
      'GET /api/entity/structural-objects/so-1/identifiers': () => xml(
        '<IdentifiersResponse><Identifiers>' +
        '<Identifier><ApiId>a1</ApiId><Type>argonath</Type><Value>public-showcase-dev</Value></Identifier>' +
        '<Identifier><ApiId>a2</ApiId><Type>code</Type><Value>x</Value></Identifier>' +
        '</Identifiers></IdentifiersResponse>'),
      'DELETE /api/entity/structural-objects/so-1/identifiers/': () => xml('', 204),
    });
    const w = writer();
    expect(await w.addIdentifier('SO', 'so-1', 'argonath', 'public-showcase-dev')).toBe('a1');
    expect(await w.deleteIdentifiers('SO', 'so-1', { idType: 'argonath' })).toBe(1);
    expect(calls.filter(c => c.method === 'DELETE').map(c => c.path))
      .toEqual(['/api/entity/structural-objects/so-1/identifiers/a1']);
  });
});

describe('findByIdentifier', () => {
  it('reads entities from a 9.x EntitiesResponse', async () => {
    stubFetch({
      'POST /api/accesstoken/login': login,
      'GET /api/entity/entities/by-identifier': () => xml(
        '<EntitiesResponse xmlns="http://preservica.com/EntityAPI/v9.4"><Entities>' +
        '<Entity title="T" ref="io-1" type="IO">https://x/api/entity/information-objects/io-1</Entity>' +
        '</Entities><Paging><TotalResults>1</TotalResults></Paging></EntitiesResponse>'),
    });
    expect(await writer().findByIdentifier('ark', 'ark:/99999/fk4x')).toEqual([{ ref: 'io-1', title: 'T', type: 'IO' }]);
  });
});

describe('updateEntity and setMetadata', () => {
  const entityXml = '<EntityResponse><InformationObject><Ref>io-1</Ref><Title>Old</Title><Description>d</Description>' +
    '<SecurityTag>open</SecurityTag><Parent>so-1</Parent></InformationObject>' +
    '<AdditionalInformation><Metadata><Fragment schema="http://www.openarchives.org/OAI/2.0/oai_dc/">' +
    'https://preservica.test/api/entity/information-objects/io-1/metadata/m-9</Fragment></Metadata></AdditionalInformation></EntityResponse>';

  it('keeps unchanged fields when updating the title', async () => {
    const calls = stubFetch({
      'POST /api/accesstoken/login': login,
      'GET /api/entity/versiondetails/version': () => xml(VERSION_XML),
      'GET /api/entity/information-objects/io-1': () => xml(entityXml),
      'PUT /api/entity/information-objects/io-1': () => xml(entityXml),
    });
    await writer().updateEntity('IO', 'io-1', { title: 'New & improved' });
    const put = calls.find(c => c.method === 'PUT');
    expect(put.init.body).toContain('<Title>New &amp; improved</Title>');
    expect(put.init.body).toContain('<Description>d</Description>');
    expect(put.init.body).toContain('<Parent>so-1</Parent>');
  });

  it('updates an existing oai_dc fragment in place', async () => {
    const calls = stubFetch({
      'POST /api/accesstoken/login': login,
      'GET /api/entity/versiondetails/version': () => xml(VERSION_XML),
      'GET /api/entity/information-objects/io-1': () => xml(entityXml),
      'PUT /api/entity/information-objects/io-1/metadata/m-9': () => xml(''),
    });
    await writer().setMetadata('IO', 'io-1', oaiDc({ title: 'New' }));
    const put = calls.find(c => c.method === 'PUT');
    expect(put.init.body).toContain('<xip:Ref>m-9</xip:Ref>');
    expect(put.init.body).toContain('<dc:title>New</dc:title>');
  });
});

describe('deleteEntity', () => {
  it('finishes without approval when the action completes on its own (Starter tier)', async () => {
    let polls = 0;
    const calls = stubFetch({
      'POST /api/accesstoken/login': login,
      'GET /api/entity/versiondetails/version': () => xml(VERSION_XML),
      'DELETE /api/entity/structural-objects/so-1': () => xml('prog-2', 202),
      'GET /api/entity/progress/prog-2': () => {
        polls += 1;
        return xml(`<ProgressStatus xmlns="http://status.preservica.com"><Status>${polls < 3 ? 'ACTIVE' : 'COMPLETED'}</Status></ProgressStatus>`);
      },
    });
    expect(await writer().deleteEntity('SO', 'so-1', { interval: 1 })).toBe('COMPLETED');
    expect(calls.some(c => c.method === 'PUT')).toBe(false);
  });

  it('approves a pending deletion with the manager login', async () => {
    let polls = 0;
    const calls = stubFetch({
      'POST /api/accesstoken/login': (url, init) => new Response(JSON.stringify({
        token: init.body.get('username') === 'mgr' ? 'tok-mgr' : 'tok-op',
      })),
      'GET /api/entity/versiondetails/version': () => xml(VERSION_XML),
      'DELETE /api/entity/information-objects/io-1': () => xml('prog-1', 202),
      'GET /api/entity/progress/prog-1': () => {
        polls += 1;
        return xml(`<ProgressStatus xmlns="http://status.preservica.com"><Status>${polls === 1 ? 'PENDING' : 'COMPLETED'}</Status></ProgressStatus>`);
      },
      'PUT /api/entity/actions/deletions/prog-1': () => xml('', 202),
    });
    expect(await writer().deleteEntity('IO', 'io-1', { interval: 1 })).toBe('COMPLETED');
    const approve = calls.find(c => c.method === 'PUT');
    expect(approve.init.headers['Preservica-Access-Token']).toBe('tok-mgr');
    expect(approve.init.body).toContain('<Approved>true</Approved>');
    const del = calls.find(c => c.method === 'DELETE');
    expect(del.init.body).toContain('xmlns="http://preservica.com/EntityAPI/v7.5"');
  });

  it('throws when the action fails instead of polling forever', async () => {
    stubFetch({
      'POST /api/accesstoken/login': login,
      'GET /api/entity/progress/p': () => xml('<ProgressStatus xmlns="http://status.preservica.com"><Status>FAILED</Status></ProgressStatus>'),
    });
    await expect(writer().waitForProgress('p', { interval: 1 })).rejects.toThrow(/ended FAILED/);
  });
});

describe('ingestAsset', () => {
  it('uploads a zip to the listed package bucket with the parent folder metadata', async () => {
    stubFetch({ 'POST /api/accesstoken/login': login });
    const sent = [];
    const s3Client = config => ({
      config,
      send: async command => {
        if (command.constructor.name === 'ListBucketsCommand') {
          return { Buckets: [{ Name: 't1abc.file.upload' }, { Name: 't1abc.package.upload' }] };
        }
        sent.push({ config, input: command.input });
        return { $metadata: {} };
      },
    });
    const result = await writer({ s3Client }).ingestAsset({
      parent: 'so-1', title: 'Item 1',
      files: [{ filename: 'tiny-1.jpg', data: Buffer.from('jpeg-bytes') }],
      identifiers: [{ type: 'ark', value: 'ark:/99999/fk4e2e1' }],
      dc: { title: 'Item 1' },
    });
    expect(sent).toHaveLength(1);
    const { config, input } = sent[0];
    expect(config.endpoint).toBe(`${BASE}/api/s3/buckets`);
    expect(config.forcePathStyle).toBe(true);
    expect(config.credentials.accessKeyId).toBe('tok-op');
    expect(input.Bucket).toBe('t1abc.package.upload');
    expect(input.Metadata).toEqual({ structuralobjectreference: 'so-1' });
    expect(input.Key).toMatch(/\.zip$/);
    expect(input.Body.subarray(0, 2).toString()).toBe('PK');
    expect(result.packageRef).toMatch(/^[0-9a-f-]{36}$/);
  });
});

describe('s3 token expiry', () => {
  it('re-logs in and retries the upload once on AccessDenied', async () => {
    let logins = 0;
    stubFetch({ 'POST /api/accesstoken/login': () => new Response(JSON.stringify({ token: `tok-${++logins}` })) });
    const keys = [];
    const s3Client = config => ({
      send: async command => {
        if (command.constructor.name === 'ListBucketsCommand') return { Buckets: [{ Name: 't1.package.upload' }] };
        keys.push(config.credentials.accessKeyId);
        if (config.credentials.accessKeyId === 'tok-1') {
          throw Object.assign(new Error('Access Denied'), { name: 'AccessDenied', $metadata: { httpStatusCode: 403 } });
        }
        return { $metadata: {} };
      },
    });
    const w = writer({ s3Client });
    await w.ingestAsset({ parent: 'so-1', title: 'T', files: [{ filename: 'a.jpg', data: Buffer.from('x') }] });
    expect(keys).toEqual(['tok-1', 'tok-2']);
  });
});

describe('package building', () => {
  it('builds an XIP with the asset, file fixity, identifiers and oai_dc', () => {
    const out = buildAssetXip({
      ioRef: 'io-1', parent: 'so-1', title: 'T',
      files: [{ coRef: 'co-1', filename: 'a.jpg', size: 3, sha1: 'abc' }],
      identifiers: [{ type: 'ark', value: 'ark:/99999/fk4x' }],
      metadata: [{ schema: 'http://www.openarchives.org/OAI/2.0/oai_dc/', xml: oaiDc({ title: 'T' }) }],
    });
    expect(out).toContain('xmlns:xip="http://preservica.com/XIP/v6.0"');
    expect(out).toContain('<xip:Parent>so-1</xip:Parent>');
    expect(out).toContain('<xip:Bitstream>p1/a.jpg</xip:Bitstream>');
    expect(out).toContain('<xip:FixityValue>abc</xip:FixityValue>');
    expect(out).toContain('<xip:Value>ark:/99999/fk4x</xip:Value>');
    expect(out).toContain('<dc:title>T</dc:title>');
  });

  it('zips metadata.xml and content under the asset ref', async () => {
    const zip = await zipAssetPackage({
      ioRef: 'io-1', parent: 'so-1', title: 'T',
      files: [{ coRef: 'co-1', filename: 'a.jpg', size: 3, sha1: 'abc', data: Buffer.from('abc') }],
    });
    const text = zip.toString('latin1');
    expect(text).toContain('io-1/metadata.xml');
    expect(text).toContain('io-1/content/p1/a.jpg');
  });
});
