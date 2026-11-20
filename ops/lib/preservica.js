/**
 * Preservica write client: create folders, ingest assets, edit identifiers,
 * metadata and content, and delete entities. Built for the e2e suites, which
 * need to make real changes for argonath's sensors to pick up. digtk's own
 * Python client (exec/digtk/lib/preservica/client.py) only reads.
 *
 * REST calls use fetch(). Asset ingest goes through Preservica's
 * S3-compatible upload endpoint (/api/s3/buckets), which needs SigV4
 * signing, so that one call uses @aws-sdk/client-s3. Endpoints and XML
 * shapes follow pyPreservica 4.3.0.
 *
 * @module preservica
 */
import crypto from 'node:crypto';
import { PassThrough } from 'node:stream';
import { ZipArchive } from 'archiver';
import { XMLParser } from 'fast-xml-parser';
import { S3Client, PutObjectCommand, ListBucketsCommand } from '@aws-sdk/client-s3';

const OAI_DC_SCHEMA = 'http://www.openarchives.org/OAI/2.0/oai_dc/';
const PACKAGE_XIP_NS = 'http://preservica.com/XIP/v6.0';

const PATHS = { SO: 'structural-objects', IO: 'information-objects', CO: 'content-objects' };
const TAGS = { SO: 'StructuralObject', IO: 'InformationObject', CO: 'ContentObject' };

const parser = new XMLParser({
  ignoreAttributes: false,
  attributeNamePrefix: '@_',
  removeNSPrefix: true,
  parseTagValue: false,
  isArray: name => ['Identifier', 'Child', 'Entity', 'Fragment', 'Representation', 'ContentObject'].includes(name),
});

/**
 * Escape text for an XML element body or attribute.
 *
 * @param {*} value - Value to escape; null/undefined become ''.
 * @returns {string} Escaped text.
 */
function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&apos;');
}

/**
 * Map an entity type ('SO', 'IO', 'CO') to its Entity API path segment.
 *
 * @param {string} type - Entity type.
 * @returns {string} e.g. 'structural-objects'.
 * @throws {Error} On an unknown type.
 */
function entityPath(type) {
  if (!PATHS[type]) throw new Error(`Unknown Preservica entity type '${type}' (use SO, IO or CO)`);
  return PATHS[type];
}

/**
 * Build a simple oai_dc metadata fragment.
 *
 * @param {Object} [fields={}] - Dublin Core fields, e.g. {title, description}. Arrays repeat the element.
 * @returns {string} oai_dc:dc XML.
 */
function oaiDc(fields = {}) {
  const body = Object.entries(fields)
    .flatMap(([name, value]) => (Array.isArray(value) ? value : [value]).map(v => `<dc:${name}>${esc(v)}</dc:${name}>`))
    .join('');
  return '<oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" ' +
    `xmlns:dc="http://purl.org/dc/elements/1.1/">${body}</oai_dc:dc>`;
}

/**
 * Build the XIP v6.0 metadata.xml for a single-asset package: one asset, one
 * Preservation representation, one content object per file.
 *
 * @param {Object} asset - Asset description.
 * @param {string} asset.ioRef - Asset UUID.
 * @param {string} asset.parent - Parent folder ref.
 * @param {string} asset.title - Asset title.
 * @param {string} [asset.description] - Asset description.
 * @param {string} [asset.securityTag='open'] - Security tag.
 * @param {Array<Object>} asset.files - [{coRef, filename, size, sha1}].
 * @param {Array<Object>} [asset.identifiers=[]] - [{type, value}].
 * @param {Array<Object>} [asset.metadata=[]] - [{schema, xml}] descriptive fragments.
 * @returns {string} metadata.xml content.
 */
function buildAssetXip(asset) {
  const tag = asset.securityTag || 'open';
  const files = asset.files.map(f => `
  <xip:ContentObject>
    <xip:Ref>${f.coRef}</xip:Ref><xip:Title>${esc(f.filename)}</xip:Title><xip:Description/>
    <xip:SecurityTag>${esc(tag)}</xip:SecurityTag><xip:CustomType/><xip:Parent>${asset.ioRef}</xip:Parent>
  </xip:ContentObject>
  <xip:Generation original="true" active="true">
    <xip:ContentObject>${f.coRef}</xip:ContentObject><xip:Label>${esc(f.filename.replace(/\.[^.]+$/, ''))}</xip:Label>
    <xip:EffectiveDate>${new Date().toISOString()}</xip:EffectiveDate>
    <xip:Bitstreams><xip:Bitstream>p1/${esc(f.filename)}</xip:Bitstream></xip:Bitstreams>
    <xip:Formats/><xip:Properties/>
  </xip:Generation>
  <xip:Bitstream>
    <xip:Filename>${esc(f.filename)}</xip:Filename><xip:FileSize>${f.size}</xip:FileSize><xip:PhysicalLocation>p1</xip:PhysicalLocation>
    <xip:Fixities><xip:Fixity><xip:FixityAlgorithmRef>SHA1</xip:FixityAlgorithmRef><xip:FixityValue>${f.sha1}</xip:FixityValue></xip:Fixity></xip:Fixities>
  </xip:Bitstream>`).join('');

  const identifiers = (asset.identifiers || []).map(i => `
  <xip:Identifier><xip:Type>${esc(i.type)}</xip:Type><xip:Value>${esc(i.value)}</xip:Value><xip:Entity>${asset.ioRef}</xip:Entity></xip:Identifier>`).join('');

  const metadata = (asset.metadata || []).map(m => `
  <xip:Metadata schemaUri="${esc(m.schema)}">
    <xip:Ref>${crypto.randomUUID()}</xip:Ref><xip:Entity>${asset.ioRef}</xip:Entity><xip:Content>${m.xml}</xip:Content>
  </xip:Metadata>`).join('');

  return `<?xml version="1.0" encoding="UTF-8"?>
<xip:XIP xmlns:xip="${PACKAGE_XIP_NS}">
  <xip:InformationObject>
    <xip:Ref>${asset.ioRef}</xip:Ref><xip:Title>${esc(asset.title)}</xip:Title><xip:Description>${esc(asset.description)}</xip:Description>
    <xip:SecurityTag>${esc(tag)}</xip:SecurityTag><xip:CustomType/><xip:Parent>${esc(asset.parent)}</xip:Parent>
  </xip:InformationObject>
  <xip:Representation>
    <xip:InformationObject>${asset.ioRef}</xip:InformationObject><xip:Name>Preservation</xip:Name><xip:Type>Preservation</xip:Type>
    <xip:ContentObjects>${asset.files.map(f => `<xip:ContentObject>${f.coRef}</xip:ContentObject>`).join('')}</xip:ContentObjects>
  </xip:Representation>${files}${identifiers}${metadata}
</xip:XIP>
`;
}

/**
 * Zip a single-asset package in memory: <ioRef>/metadata.xml plus
 * <ioRef>/content/p1/<filename> for each file.
 *
 * @param {Object} asset - Same shape as buildAssetXip(), with each file's `data` (Buffer).
 * @returns {Promise<Buffer>} The zip bytes.
 */
async function zipAssetPackage(asset) {
  const archive = new ZipArchive({ zlib: { level: 6 } });
  const out = new PassThrough();
  const chunks = [];
  out.on('data', chunk => chunks.push(chunk));
  const done = new Promise((resolve, reject) => {
    out.on('end', resolve);
    archive.on('error', reject);
  });
  archive.pipe(out);
  archive.append(buildAssetXip(asset), { name: `${asset.ioRef}/metadata.xml` });
  for (const f of asset.files) {
    archive.append(f.data, { name: `${asset.ioRef}/content/p1/${f.filename}` });
  }
  await archive.finalize();
  await done;
  return Buffer.concat(chunks);
}

/**
 * Preservica Entity API client for writes, logged in as an operator user,
 * plus an optional manager user to approve deletions.
 */
class PreservicaWriter {

  /**
   * @param {Object} [opts={}] - Connection options; each falls back to the env var in brackets.
   * @param {string} [opts.baseUrl] - [PRESERVICA_BASE_URL], else https://<tenant>.preservica.com (as digtk).
   * @param {string} [opts.tenant] - Tenant [PRESERVICA_TENANT].
   * @param {string} [opts.packageBucket] - Upload bucket [PRESERVICA_PACKAGE_BUCKET], else found via ListBuckets.
   * @param {string} [opts.username] - Operator user [PRESERVICA_USERNAME].
   * @param {string} [opts.password] - Operator password [PRESERVICA_PASSWORD].
   * @param {string} [opts.managerUsername] - Deletion approver [PRESERVICA_MANAGER_USERNAME], else the operator.
   * @param {string} [opts.managerPassword] - Approver password [PRESERVICA_MANAGER_PASSWORD], else the operator's.
   * @param {Function} [opts.s3Client] - Factory (config) => S3 client, for tests.
   */
  constructor(opts = {}) {
    const env = process.env;
    const tenant = opts.tenant || env.PRESERVICA_TENANT || '';
    const baseUrl = opts.baseUrl || env.PRESERVICA_BASE_URL || (tenant ? `https://${tenant}.preservica.com` : '');
    if (!baseUrl) throw new Error('Preservica base URL or tenant is required');
    this.baseUrl = baseUrl.replace(/\/+$/, '');
    this.tenant = tenant;
    this.username = opts.username || env.PRESERVICA_USERNAME;
    this.password = opts.password || env.PRESERVICA_PASSWORD;
    this.managerUsername = opts.managerUsername || env.PRESERVICA_MANAGER_USERNAME || this.username;
    this.managerPassword = opts.managerPassword || env.PRESERVICA_MANAGER_PASSWORD || this.password;
    this.s3Factory = opts.s3Client || (config => new S3Client(config));
    this.token = null;
    this.ns = null;
    this.bucket = opts.packageBucket || env.PRESERVICA_PACKAGE_BUCKET || null;
  }

  /**
   * Send one S3 command to Preservica's upload endpoint. The access token is
   * the S3 key, and tokens expire (~15 minutes), so an AccessDenied gets one
   * retry with a fresh login.
   *
   * @param {Object} command - An @aws-sdk/client-s3 command.
   * @returns {Promise<Object>} The command's result.
   */
  async s3Send(command) {
    try {
      return await (await this.s3()).send(command);
    } catch (e) {
      if (e?.name !== 'AccessDenied' && e?.$metadata?.httpStatusCode !== 403) throw e;
      await this.login();
      return (await this.s3()).send(command);
    }
  }

  /**
   * An S3 client for Preservica's S3-compatible upload endpoint, keyed by the access token.
   *
   * @returns {Promise<Object>} S3 client.
   */
  async s3() {
    const token = this.token || await this.login();
    return this.s3Factory({
      endpoint: `${this.baseUrl}/api/s3/buckets`,
      region: 'us-east-1',
      forcePathStyle: true,
      credentials: { accessKeyId: token, secretAccessKey: 'NOT_USED' },
    });
  }

  /**
   * The package upload bucket. Its name uses the tenant's internal id, which
   * can differ from the tenant name in the hostname (Starter tier, e.g.
   * 't1b208e31adfde268.package.upload'), so it is looked up with ListBuckets.
   *
   * @returns {Promise<string>} Bucket name.
   * @throws {Error} If no '*.package.upload' bucket is listed.
   */
  async packageBucket() {
    if (this.bucket) return this.bucket;
    const out = await this.s3Send(new ListBucketsCommand({}));
    const bucket = (out?.Buckets || []).map(b => b.Name).find(name => name?.endsWith('.package.upload'));
    if (!bucket) throw new Error(`No *.package.upload bucket at ${this.baseUrl}/api/s3/buckets`);
    this.bucket = bucket;
    return bucket;
  }

  /**
   * Log in and return an access token. The tenant is not sent: the Starter
   * tier takes it from the hostname and rejects an explicit one with
   * 'invalid.user incorrect.tenant' (see digtk's lib/preservica/client.py).
   *
   * @param {Object} [opts={}] - Options.
   * @param {boolean} [opts.manager=false] - Log in as the manager user instead of the operator.
   * @returns {Promise<string>} Access token.
   * @throws {Error} On missing credentials or a failed login.
   */
  async login(opts = {}) {
    const username = opts.manager ? this.managerUsername : this.username;
    const password = opts.manager ? this.managerPassword : this.password;
    if (!username || !password) {
      throw new Error(`Preservica ${opts.manager ? 'manager' : 'operator'} username/password not set`);
    }
    const body = new URLSearchParams({ username, password });
    const res = await fetch(`${this.baseUrl}/api/accesstoken/login`, { method: 'POST', body });
    const data = await res.json().catch(() => null);
    if (!res.ok || !data?.token || data.success === false) {
      throw new Error(`Preservica login failed for ${username}: HTTP ${res.status} ${data?.message || ''}`.trim());
    }
    if (!opts.manager) this.token = data.token;
    return data.token;
  }

  /**
   * Send an authenticated request, logging in first if needed and once more on a 401.
   *
   * @param {string} method - HTTP method.
   * @param {string} path - Path under the base URL, e.g. '/api/entity/...'.
   * @param {Object} [opts={}] - Options.
   * @param {string|Buffer} [opts.body] - Request body.
   * @param {Object} [opts.headers] - Extra headers.
   * @param {string} [opts.token] - Use this token instead of the operator's.
   * @param {number[]} [opts.expect] - Accepted statuses (default any 2xx).
   * @returns {Promise<Response>} The response.
   * @throws {Error} On an unexpected status.
   */
  async request(method, path, opts = {}) {
    const send = async token => fetch(`${this.baseUrl}${path}`, {
      method,
      body: opts.body,
      headers: { 'Preservica-Access-Token': token, ...(opts.headers || {}) },
    });
    let token = opts.token || this.token || await this.login();
    let res = await send(token);
    if (res.status === 401 && !opts.token) {
      token = await this.login();
      res = await send(token);
    }
    const ok = opts.expect ? opts.expect.includes(res.status) : res.ok;
    if (!ok) {
      const text = await res.text().catch(() => '');
      throw new Error(`Preservica ${method} ${path} failed: HTTP ${res.status} ${text.slice(0, 500)}`);
    }
    return res;
  }

  /**
   * Send an XML request and parse the XML response (null for an empty body).
   *
   * @param {string} method - HTTP method.
   * @param {string} path - Path under the base URL.
   * @param {Object} [opts={}] - request() options; a string `body` is sent as XML.
   * @returns {Promise<Object|null>} Parsed XML, namespace prefixes removed.
   */
  async xml(method, path, opts = {}) {
    const headers = opts.body ? { 'Content-Type': 'application/xml;charset=UTF-8', ...(opts.headers || {}) } : opts.headers;
    const res = await this.request(method, path, { ...opts, headers });
    const text = await res.text();
    return text.trim() ? parser.parse(text) : null;
  }

  /**
   * The XIP and EntityAPI namespaces for this server's version.
   *
   * @returns {Promise<{xip: string, entity: string}>} Namespace URIs.
   */
  async namespaces() {
    if (this.ns) return this.ns;
    const res = await this.request('GET', '/api/entity/versiondetails/version');
    const text = await res.text();
    const match = text.match(/<CurrentVersion>(\d+)\.(\d+)/);
    if (!match) throw new Error(`Could not read Preservica version from: ${text.slice(0, 200)}`);
    const [major, minor] = [Number(match[1]), Number(match[2])];
    const v = major === 6 && minor < 2 ? 'v6.0' : `v${major}.${minor}`;
    this.ns = { xip: `http://preservica.com/XIP/${v}`, entity: `http://preservica.com/EntityAPI/${v}` };
    return this.ns;
  }

  /**
   * Fetch one entity's core fields.
   *
   * @param {string} type - 'SO', 'IO' or 'CO'.
   * @param {string} ref - Entity ref.
   * @returns {Promise<Object>} {ref, title, description, securityTag, parent, fragments: [{url, schema}]}.
   */
  async getEntity(type, ref) {
    const doc = await this.xml('GET', `/api/entity/${entityPath(type)}/${ref}`);
    const root = doc.EntityResponse || doc;
    const entity = root[TAGS[type]] || {};
    // 9.x lists fragments under AdditionalInformation/Metadata (only when some exist)
    const metadata = root.AdditionalInformation?.Metadata || root.Metadata || {};
    const fragments = (metadata.Fragment || []).map(f => ({ url: f['#text'], schema: f['@_schema'] }));
    return {
      ref: entity.Ref, title: entity.Title, description: entity.Description,
      securityTag: entity.SecurityTag, parent: entity.Parent, fragments,
    };
  }

  /**
   * Whether an entity exists (a 404 means it does not, or it was deleted).
   *
   * @param {string} type - 'SO', 'IO' or 'CO'.
   * @param {string} ref - Entity ref.
   * @returns {Promise<boolean>} True if it exists.
   */
  async exists(type, ref) {
    const res = await this.request('GET', `/api/entity/${entityPath(type)}/${ref}`, { expect: [200, 404] });
    return res.status === 200;
  }

  /**
   * List a folder's direct children (all pages).
   *
   * @param {string} ref - Folder ref.
   * @returns {Promise<Array<Object>>} [{ref, title, type}].
   */
  async getChildren(ref) {
    const children = [];
    let path = `/api/entity/structural-objects/${ref}/children?start=0&max=100`;
    while (path) {
      const doc = await this.xml('GET', path);
      const root = doc.ChildrenResponse || doc;
      for (const c of root.Children?.Child || []) {
        children.push({ ref: c['@_ref'], title: c['@_title'], type: c['@_type'] });
      }
      const next = root.Paging?.Next;
      path = next ? next.replace(this.baseUrl, '') : null;
    }
    return children;
  }

  /**
   * Find entities by identifier.
   *
   * @param {string} idType - Identifier type.
   * @param {string} value - Identifier value.
   * @returns {Promise<Array<Object>>} [{ref, title, type}].
   */
  async findByIdentifier(idType, value) {
    const query = new URLSearchParams({ type: idType, value });
    const doc = await this.xml('GET', `/api/entity/entities/by-identifier?${query}`);
    const root = doc?.EntitiesResponse || doc || {};
    return (root.Entities?.Entity || root.Entity || []).map(e => ({ ref: e['@_ref'], title: e['@_title'], type: e['@_type'] }));
  }

  /**
   * Create a folder.
   *
   * @param {string} title - Folder title.
   * @param {Object} [opts={}] - Options.
   * @param {string} [opts.parent] - Parent folder ref (else the tenant root).
   * @param {string} [opts.description] - Description.
   * @param {string} [opts.securityTag='open'] - Security tag.
   * @returns {Promise<string>} The new folder's ref.
   */
  async createFolder(title, opts = {}) {
    const { xip } = await this.namespaces();
    const ref = crypto.randomUUID();
    const body = `<StructuralObject xmlns="${xip}"><Ref>${ref}</Ref><Title>${esc(title)}</Title>` +
      `<Description>${esc(opts.description)}</Description><SecurityTag>${esc(opts.securityTag || 'open')}</SecurityTag>` +
      (opts.parent ? `<Parent>${esc(opts.parent)}</Parent>` : '') + '</StructuralObject>';
    const doc = await this.xml('POST', '/api/entity/structural-objects', { body });
    const root = doc?.EntityResponse || doc || {};
    return root.StructuralObject?.Ref || ref;
  }

  /**
   * Add an identifier to an entity.
   *
   * @param {string} type - 'SO', 'IO' or 'CO'.
   * @param {string} ref - Entity ref.
   * @param {string} idType - Identifier type, e.g. 'argonath'.
   * @param {string} value - Identifier value.
   * @returns {Promise<string|undefined>} The identifier's ApiId.
   */
  async addIdentifier(type, ref, idType, value) {
    const { xip } = await this.namespaces();
    const body = `<Identifier xmlns="${xip}"><Type>${esc(idType)}</Type><Value>${esc(value)}</Value><Entity>${ref}</Entity></Identifier>`;
    const doc = await this.xml('POST', `/api/entity/${entityPath(type)}/${ref}/identifiers`, { body });
    return doc?.IdentifiersResponse?.Identifier?.[0]?.ApiId ?? doc?.Identifier?.[0]?.ApiId;
  }

  /**
   * List an entity's identifiers.
   *
   * @param {string} type - 'SO', 'IO' or 'CO'.
   * @param {string} ref - Entity ref.
   * @returns {Promise<Array<Object>>} [{type, value, apiId}].
   */
  async getIdentifiers(type, ref) {
    const doc = await this.xml('GET', `/api/entity/${entityPath(type)}/${ref}/identifiers`);
    const root = doc?.IdentifiersResponse || doc || {};
    const list = root.Identifiers?.Identifier || root.Identifier || [];
    return list.map(i => ({ type: i.Type, value: i.Value, apiId: i.ApiId }));
  }

  /**
   * Delete an entity's identifiers matching a type and/or value.
   *
   * @param {string} type - 'SO', 'IO' or 'CO'.
   * @param {string} ref - Entity ref.
   * @param {Object} [opts={}] - Filters; with neither, every identifier is deleted.
   * @param {string} [opts.idType] - Only this identifier type.
   * @param {string} [opts.value] - Only this value.
   * @returns {Promise<number>} Number deleted.
   */
  async deleteIdentifiers(type, ref, opts = {}) {
    const matches = (await this.getIdentifiers(type, ref)).filter(i =>
      (!opts.idType || i.type === opts.idType) && (!opts.value || i.value === opts.value));
    for (const i of matches) {
      await this.request('DELETE', `/api/entity/${entityPath(type)}/${ref}/identifiers/${i.apiId}`);
    }
    return matches.length;
  }

  /**
   * Update an entity's title and/or description, keeping its other fields.
   *
   * @param {string} type - 'SO' or 'IO'.
   * @param {string} ref - Entity ref.
   * @param {Object} [opts={}] - New values; omitted fields keep their current value.
   * @param {string} [opts.title] - New title.
   * @param {string} [opts.description] - New description.
   * @returns {Promise<void>}
   */
  async updateEntity(type, ref, opts = {}) {
    const { xip } = await this.namespaces();
    const current = await this.getEntity(type, ref);
    const tag = TAGS[type];
    const body = `<${tag} xmlns="${xip}"><Ref>${ref}</Ref>` +
      `<Title>${esc(opts.title ?? current.title)}</Title>` +
      `<Description>${esc(opts.description ?? current.description)}</Description>` +
      `<SecurityTag>${esc(current.securityTag || 'open')}</SecurityTag>` +
      (current.parent ? `<Parent>${esc(current.parent)}</Parent>` : '') + `</${tag}>`;
    await this.xml('PUT', `/api/entity/${entityPath(type)}/${ref}`, { body });
  }

  /**
   * Replace an entity's descriptive metadata for one schema, adding it if absent.
   *
   * @param {string} type - 'SO' or 'IO'.
   * @param {string} ref - Entity ref.
   * @param {string} xml - The metadata document's root element, e.g. from oaiDc().
   * @param {Object} [opts={}] - Options.
   * @param {string} [opts.schema=OAI_DC_SCHEMA] - Schema URI.
   * @returns {Promise<void>}
   */
  async setMetadata(type, ref, xml, opts = {}) {
    const schema = opts.schema || OAI_DC_SCHEMA;
    const { xip } = await this.namespaces();
    const current = await this.getEntity(type, ref);
    const fragment = current.fragments.find(f => f.schema === schema);
    const path = `/api/entity/${entityPath(type)}/${ref}/metadata`;
    if (!fragment) {
      const body = `<xip:MetadataContainer schemaUri="${esc(schema)}" xmlns:xip="${xip}">` +
        `<xip:Entity>${ref}</xip:Entity><xip:Content>${xml}</xip:Content></xip:MetadataContainer>`;
      await this.xml('POST', path, { body });
      return;
    }
    const mref = fragment.url.split(`${ref}/metadata/`)[1];
    const body = `<xip:MetadataContainer schemaUri="${esc(schema)}" xmlns:xip="${xip}">` +
      `<xip:Ref>${mref}</xip:Ref><xip:Entity>${ref}</xip:Entity><xip:Content>${xml}</xip:Content></xip:MetadataContainer>`;
    await this.xml('PUT', `${path}/${mref}`, { body });
  }

  /**
   * Ingest one asset (an information object with one or more files) into a
   * folder, through Preservica's S3-compatible package upload. Ingest is
   * asynchronous: use waitForIdentifier() with one of `opts.identifiers` to
   * find the new asset once it lands.
   *
   * @param {Object} opts - Asset.
   * @param {string} opts.parent - Folder ref to ingest into.
   * @param {string} opts.title - Asset title.
   * @param {string} [opts.description] - Asset description.
   * @param {Array<Object>} opts.files - [{filename, data (Buffer)}].
   * @param {Array<Object>} [opts.identifiers=[]] - [{type, value}] to attach.
   * @param {Object} [opts.dc] - oai_dc fields to attach as descriptive metadata.
   * @returns {Promise<Object>} {packageRef (the package's asset UUID), key (S3 key), progressToken}.
   */
  async ingestAsset(opts) {
    const ioRef = crypto.randomUUID();
    const files = opts.files.map(f => ({
      ...f,
      coRef: crypto.randomUUID(),
      size: f.data.length,
      sha1: crypto.createHash('sha1').update(f.data).digest('hex'),
    }));
    const zip = await zipAssetPackage({
      ioRef, parent: opts.parent, title: opts.title, description: opts.description,
      files, identifiers: opts.identifiers,
      metadata: opts.dc ? [{ schema: OAI_DC_SCHEMA, xml: oaiDc(opts.dc) }] : [],
    });

    const bucket = await this.packageBucket();
    const key = `${crypto.randomUUID()}.zip`;
    const result = await this.s3Send(new PutObjectCommand({
      Bucket: bucket,
      Key: key,
      Body: zip,
      Metadata: { structuralobjectreference: opts.parent },
    }));
    const headers = result?.$metadata?.httpHeaders || result?.ResponseMetadata?.HTTPHeaders || {};
    return { packageRef: ioRef, key, progressToken: headers['preservica-progress-token'] || null };
  }

  /**
   * Poll until an entity with this identifier exists (e.g. after ingest).
   *
   * @param {string} idType - Identifier type.
   * @param {string} value - Identifier value.
   * @param {Object} [opts={}] - Options.
   * @param {number} [opts.timeout=600000] - Give up after this many ms.
   * @param {number} [opts.interval=10000] - Poll interval in ms.
   * @returns {Promise<Object>} The first match, {ref, title, type}.
   * @throws {Error} On timeout.
   */
  async waitForIdentifier(idType, value, opts = {}) {
    const timeout = opts.timeout ?? 600000;
    const interval = opts.interval ?? 10000;
    const start = Date.now();
    for (;;) {
      const found = await this.findByIdentifier(idType, value);
      if (found.length) return found[0];
      if (Date.now() - start > timeout) {
        throw new Error(`No Preservica entity with identifier ${idType}=${value} after ${timeout}ms`);
      }
      await new Promise(resolve => setTimeout(resolve, interval));
    }
  }

  /**
   * An asset's content object refs, across all representations.
   *
   * @param {string} ioRef - Asset ref.
   * @returns {Promise<string[]>} Content object refs.
   */
  async contentObjects(ioRef) {
    const doc = await this.xml('GET', `/api/entity/information-objects/${ioRef}/representations`);
    const root = doc?.RepresentationsResponse || doc || {};
    const urls = (root.Representations?.Representation || []).map(r => (typeof r === 'string' ? r : r['#text']));
    const refs = [];
    for (const url of urls) {
      const rep = await this.xml('GET', url.replace(this.baseUrl, ''));
      const repRoot = rep?.RepresentationResponse || rep || {};
      for (const co of repRoot.ContentObjects?.ContentObject || []) {
        refs.push(typeof co === 'string' ? co : co['@_ref'] || co['#text']);
      }
    }
    return refs;
  }

  /**
   * Replace a content object's file with a new generation.
   *
   * @param {string} coRef - Content object ref (see contentObjects()).
   * @param {Object} file - New file.
   * @param {string} file.filename - File name.
   * @param {Buffer} file.data - File bytes.
   * @param {Object} [opts={}] - waitForProgress() options.
   * @returns {Promise<string>} Final progress status.
   */
  async replaceContent(coRef, file, opts = {}) {
    const res = await this.request('POST', `/api/entity/content-objects/${coRef}/generations?replaceType=previous`, {
      body: file.data,
      headers: {
        'Content-Type': 'application/octet-stream',
        Filename: file.filename,
        'Fixity-SHA-1': crypto.createHash('sha1').update(file.data).digest('hex'),
      },
    });
    const progressId = (await res.text()).trim();
    return this.waitForProgress(progressId, opts);
  }

  /**
   * Delete an asset or folder. Preservica holds the deletion PENDING until a
   * manager approves it, so this approves with the manager login.
   *
   * @param {string} type - 'IO' or 'SO'.
   * @param {string} ref - Entity ref.
   * @param {Object} [opts={}] - Options, plus waitForProgress() options.
   * @param {string} [opts.comment='argonath e2e cleanup'] - Operator and approval comment.
   * @returns {Promise<string>} Final progress status.
   */
  async deleteEntity(type, ref, opts = {}) {
    const { xip, entity } = await this.namespaces();
    const comment = opts.comment || 'argonath e2e cleanup';
    const body = `<DeletionAction xmlns="${entity}" xmlns:xip="${xip}"><Submission><Comment>${esc(comment)}</Comment></Submission></DeletionAction>`;
    const res = await this.request('DELETE', `/api/entity/${entityPath(type)}/${ref}`, {
      body, expect: [202],
      headers: { 'Content-Type': 'application/xml;charset=UTF-8' },
    });
    const progressId = (await res.text()).trim();
    return this.waitForProgress(progressId, {
      ...opts,
      onPending: async () => {
        const token = await this.login({ manager: true });
        const approval = `<DeletionAction xmlns="${entity}" xmlns:xip="${xip}"><Approval><Approved>true</Approved>` +
          `<Comment>${esc(comment)}</Comment></Approval></DeletionAction>`;
        await this.request('PUT', `/api/entity/actions/deletions/${progressId}`, {
          body: approval, token, expect: [202],
          headers: { 'Content-Type': 'application/xml;charset=UTF-8' },
        });
      },
    });
  }

  /**
   * Poll an asynchronous Entity API action until it finishes.
   *
   * @param {string} progressId - Progress id from the action's response.
   * @param {Object} [opts={}] - Options.
   * @param {Function} [opts.onPending] - Called once when the action is PENDING (needs approval).
   * @param {number} [opts.timeout=600000] - Give up after this many ms.
   * @param {number} [opts.interval=2000] - Poll interval in ms.
   * @returns {Promise<string>} 'COMPLETED'.
   * @throws {Error} On a failed status or timeout.
   */
  async waitForProgress(progressId, opts = {}) {
    const timeout = opts.timeout ?? 600000;
    const interval = opts.interval ?? 2000;
    const start = Date.now();
    let approved = false;
    for (;;) {
      const doc = await this.xml('GET', `/api/entity/progress/${progressId}`, { headers: { 'Content-Type': 'text/plain' } });
      // 9.x returns <ProgressStatus xmlns="http://status.preservica.com"><Status>…
      const root = doc?.ProgressStatus || doc?.ProgressResponse || doc || {};
      const status = String(root.Status ?? '').toUpperCase();
      if (status === 'COMPLETED') return status;
      if (status === 'PENDING' && opts.onPending && !approved) {
        approved = true;
        await opts.onPending();
      } else if (['FAILED', 'ERROR', 'CANCELLED', 'ABORTED', 'REJECTED'].includes(status)) {
        throw new Error(`Preservica action ${progressId} ended ${status}`);
      }
      if (Date.now() - start > timeout) {
        throw new Error(`Preservica action ${progressId} still ${status || 'unknown'} after ${timeout}ms`);
      }
      await new Promise(resolve => setTimeout(resolve, interval));
    }
  }
}

export {
  PreservicaWriter,
  buildAssetXip,
  zipAssetPackage,
  oaiDc,
  OAI_DC_SCHEMA,
};
