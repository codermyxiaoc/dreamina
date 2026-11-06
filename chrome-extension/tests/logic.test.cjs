const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const logic = require('../logic.js');
const id1 = '12345678-1234-1234-1234-123456789abc';
const id2 = '12345678-1234-1234-1234-123456789abd';
const spec = logic.validateSpec({prompt: '水彩湖面，静音，无对白。'});
const card = (id, labels = [logic.MODEL_NAME, '4s', '16:9', '720P']) => ({id, prompts: [spec.prompt], labels,
  text: spec.prompt + 'Dreamina Seedance 2.0 Mini4s16:9720P\nDetails\n78%Dreaming...'});

test('accepts exactly the supported baseline', () => {
  assert.equal(spec.model, 'seedance-2.0-mini');
  assert.equal(spec.duration, 4);
  assert.equal(logic.validateSpec({...spec, model: 'seedance-2.5'}).model,'seedance-2.5');
  assert.equal(logic.validateSpec({...spec, references: []}).references.length,0);
  assert.throws(() => logic.validateSpec({...spec, seed: 123}));
  assert.throws(() => logic.validateSpec({...spec, mode: 'frames'}));
});
test('accepts observed concatenated card text using metadata spans', () => {
  assert.equal(logic.selectNewRecord([card(id1)], [], spec).id, id1);
});
test('old matching tasks never become the new task', () => {
  assert.equal(logic.selectNewRecord([card(id1)], [id1], spec), null);
});
test('14 seconds cannot match the 4 second request', () => {
  assert.equal(logic.selectNewRecord([card(id1, [logic.MODEL_NAME, '14s', '16:9', '720P'])], [], spec), null);
});
test('different model or parameters cannot match', () => {
  assert.equal(logic.selectNewRecord([card(id1, ['Dreamina Seedance 2.0', '4s', '16:9', '720P'])], [], spec), null);
  assert.equal(logic.selectNewRecord([card(id1, [logic.MODEL_NAME, '4s', '9:16', '720P'])], [], spec), null);
});
test('multiple matching new task IDs are uncertain, not first-wins', () => {
  assert.throws(() => logic.selectNewRecord([card(id1), card(id2)], [], spec), /多个/);
});
test('nested duplicate DOM entries for the same task deduplicate', () => {
  assert.equal(logic.selectNewRecord([card(id1), card(id1)], [], spec).id, id1);
});
test('worker recovery only waits for an existing in-page execution', () => {
  assert.deepEqual(logic.recoveryAction({id: 'job1', deadline: 100}, {activeJobId: 'job1'}, 50), {action: 'wait'});
});
test('reload, missing tab or timeout never redispatches a claimed job', () => {
  for (const [state, now] of [[null, 50], [{activeJobId: null}, 50], [{activeJobId: 'other'}, 50], [{activeJobId: 'job1'}, 101]]) {
    assert.deepEqual(logic.recoveryAction({id: 'job1', deadline: 100}, state, now), {action: 'uncertain'});
  }
});
test('a completed DOM observation can be recovered without another click', () => {
  const result = {job_id: 'job1', status: 'submitted', submit_id: id1};
  assert.deepEqual(logic.recoveryAction({id: 'job1', deadline: 100}, {lastResult: result}, 101), {action: 'report', result});
});
test('uncertain claims remain locked but accept a late submitted ID', () => {
  const inflight = {id: 'job1', deadline: 100, phase: 'uncertain'};
  assert.deepEqual(logic.recoveryAction(inflight, null, 1000), {action: 'hold'});
  assert.deepEqual(logic.recoveryAction(inflight, {lastResult: {job_id: 'job1', status: 'uncertain'}}, 1000), {action: 'hold'});
  const result = {job_id: 'job1', status: 'submitted', submit_id: id1};
  assert.deepEqual(logic.recoveryAction(inflight, {lastResult: result}, 1000), {action: 'report', result});
});
test('report validation rejects a fake non-UUID submit ID', () => {
  assert.equal(logic.validResult({status: 'submitted', submit_id: 'not-a-task'}), false);
  assert.equal(logic.validResult({status: 'submitted', submit_id: id1}), true);
  assert.equal(logic.validResult({status: 'completed', submit_id: id1}), false);
});
test('manifest has narrow hosts, no Cookie/debugger access, and stable public identity', () => {
  const root = path.resolve(__dirname, '..');
  const manifest = JSON.parse(fs.readFileSync(path.join(root, 'manifest.json'), 'utf8'));
  assert.deepEqual(manifest.host_permissions, ['https://dreamina.capcut.com/*', 'http://127.0.0.1:8787/*']);
  assert.equal(manifest.permissions.includes('cookies'), false);
  assert.equal(manifest.permissions.includes('debugger'), false);
  const id = [...crypto.createHash('sha256').update(Buffer.from(manifest.key, 'base64')).digest().subarray(0, 16)]
    .map(b => String.fromCharCode(97 + (b >> 4), 97 + (b & 15))).join('');
  assert.equal(id, fs.readFileSync(path.join(root, 'extension-id.txt'), 'utf8').trim());
});
