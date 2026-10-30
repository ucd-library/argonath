import fs from 'node:fs';
import { describe, expect, it } from 'vitest';
import { OPTION_FIELDS, STEP_FIELDS, buildFanOutConfig, parseSet, parseTarget, workspaceLaunch } from '../lib/workspace.js';

describe('parseTarget', () => {
  it.each([
    'cask:/workspace/jdoe/box-12',
    'cask:/workspace/jdoe/box-12/',
    '/workspace/jdoe/box-12',
    'jdoe/box-12',
  ])('reads %s', (target) => {
    expect(parseTarget(target)).toEqual({
      username: 'jdoe', folder: 'box-12', partitionKey: 'workspace/jdoe/box-12',
    });
  });

  it.each(['cask:/workspace/jdoe', 'cask:/workspace/jdoe/box-12/source', 'box-12', ''])(
    'rejects %s', (target) => {
      expect(() => parseTarget(target)).toThrow(/workspace folder/);
    },
  );
});

describe('parseSet', () => {
  it('parses JSON values and falls back to strings', () => {
    expect(parseSet('tables=true')).toEqual(['tables', true]);
    expect(parseSet('min_conf=60')).toEqual(['min_conf', 60]);
    expect(parseSet('mode=inplace')).toEqual(['mode', 'inplace']);
    expect(parseSet('collection_context=a=b')).toEqual(['collection_context', 'a=b']);
  });

  it('needs a key', () => {
    expect(() => parseSet('=x')).toThrow(/key=value/);
    expect(() => parseSet('tables')).toThrow(/key=value/);
  });
});

describe('buildFanOutConfig', () => {
  it('is empty without overrides, so job defaults apply', () => {
    expect(buildFanOutConfig()).toEqual({});
  });

  it('ticks only the listed steps', () => {
    const config = buildFanOutConfig({ steps: 'ocr, tag-pdf' });
    expect(Object.keys(config).sort()).toEqual(Object.values(STEP_FIELDS).sort());
    expect(config.ocr).toBe(true);
    expect(config.tag_pdf).toBe(true);
    expect(config.figures).toBe(false);
    expect(config.split_detect).toBe(false);
  });

  it('rejects unknown steps', () => {
    expect(() => buildFanOutConfig({ steps: 'ocr,dance' })).toThrow(/unknown step\(s\) dance/);
  });

  it('turns on options named in --steps and leaves the rest out', () => {
    const config = buildFanOutConfig({ steps: 'ocr,vision,reuse-ocr' });
    expect(config.vision).toBe(true);
    expect(config.reuse_ocr).toBe(true);
    expect('embed_fonts' in config).toBe(false);
    expect('validate' in config).toBe(false);
  });

  it('expands all to every step', () => {
    const config = buildFanOutConfig({ steps: 'all,vision,reuse-ocr,embed-fonts,validate' });
    for (const field of Object.values(STEP_FIELDS)) expect(config[field]).toBe(true);
    for (const field of ['vision', 'reuse_ocr', 'embed_fonts', 'validate']) expect(config[field]).toBe(true);
    expect('tables' in config).toBe(false);
  });

  it('turns on any on/off option named in --steps', () => {
    const config = buildFanOutConfig({ steps: 'alt-text,want-long,want-description,tables,paragraphs,correct' });
    expect(config).toMatchObject({
      alt_text: true, want_long: true, want_description: true, tables: true, paragraphs: true, correct: true,
    });
    expect('strict' in config).toBe(false);
  });

  it('only names real boolean WorkspaceRunConfig fields', () => {
    const src = fs.readFileSync(new URL('../../dagster/lib/configs.py', import.meta.url), 'utf8');
    const body = src.split('class WorkspaceRunConfig')[1].split('\nclass ')[0];
    const bools = new Set([...body.matchAll(/^\s+(\w+): bool = Field/gm)].map(m => m[1]));
    for (const field of [...Object.values(STEP_FIELDS), ...Object.values(OPTION_FIELDS)]) {
      expect(bools.has(field), field).toBe(true);
    }
  });

  it('takes the full step list from the docs example', () => {
    const steps = 'split-detect,split-apply,ocr,ocr-correct,alt-text,figures,pdf-build,tag-pdf,'
      + 'vision,reuse-ocr,embed-fonts,validate';
    expect(buildFanOutConfig({ steps })).toEqual(buildFanOutConfig({ steps: 'all,vision,reuse-ocr,embed-fonts,validate' }));
  });

  it('layers config file, then --steps, then --set', () => {
    const config = buildFanOutConfig({
      config: { ocr: false, tables: false, max_steps: 10 },
      steps: 'ocr',
      set: ['tables=true', 'ocr=false'],
    });
    expect(config.ocr).toBe(false);
    expect(config.tables).toBe(true);
    expect(config.max_steps).toBe(10);
  });
});

describe('workspaceLaunch', () => {
  it('targets workspace_process_job on the folder partition', () => {
    expect(workspaceLaunch('cask:/workspace/jdoe/box-12', { set: ['ocr=true'] })).toEqual({
      jobName: 'workspace_process_job',
      partitionKey: 'workspace/jdoe/box-12',
      runConfig: { ops: { fan_out_workspace: { config: { ocr: true } } } },
    });
  });

  it('refuses to launch with no step selected', () => {
    expect(() => workspaceLaunch('jdoe/box-12')).toThrow(/no steps selected/);
    expect(() => workspaceLaunch('jdoe/box-12', { steps: 'vision' })).toThrow(/no steps selected/);
  });
});
