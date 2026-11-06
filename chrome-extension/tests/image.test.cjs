const test = require('node:test');
const assert = require('node:assert/strict');
const logic = require('../logic.js');

const first = '12345678-1234-1234-1234-123456789aaa';
const second = '12345678-1234-1234-1234-123456789aab';
const input = {kind: 'image', model: 'seedream-5.0-lite', prompt: '一只蓝色陶瓷花瓶。'};
const spec = logic.validateSpec(input);
const card = (id, labels = ['Seedream 5.0 Lite', '1:1', '2K'], prompt = spec.prompt) => ({
  id, prompts: [prompt], labels,
  text: `${prompt}Seedream 5.0 Lite1:12K\nDetails`,
});

test('image baseline defaults are explicit and do not mutate input', () => {
  assert.deepEqual(input, {kind: 'image', model: 'seedream-5.0-lite', prompt: '一只蓝色陶瓷花瓶。'});
  assert.equal(spec.kind, 'image');
  assert.equal(spec.model, 'seedream-5.0-lite');
  assert.equal(spec.n, 1);
  assert.equal(spec.size, '2048x2048');
  assert.equal(spec.ratio, '1:1');
  assert.equal(spec.resolution, '2K');
  assert.equal(logic.validateSpec({model: 'seedream-5.0-lite', prompt: 'Vase'}).kind, 'image');
});

test('image validator rejects unsupported model, resolution, count and references', () => {
  for (const changes of [
    {model: 'seedream-4.5'}, {n: 2}, {n: true}, {n: '1'},
    {size: '1024x1024'}, {ratio: '16:9'}, {resolution: '4K'},
    {image: 'reference.png'}, {references: []}, {seed: 12}, {mode: 'frames'},
  ]) assert.throws(() => logic.validateSpec({...input, ...changes}));
});

test('an explicit conflicting kind cannot masquerade as an image job', () => {
  for (const kind of ['video', 'other', null, false]) {
    assert.throws(() => logic.validateSpec({...input, kind}));
  }
});

test('image prompt limit counts UTF-16 units consistently with the page', () => {
  assert.equal(logic.validateSpec({...input, prompt: '😀'.repeat(1000)}).prompt.length, 2000);
  assert.throws(() => logic.validateSpec({...input, prompt: '😀'.repeat(1001)}));
  for (const prompt of ['', '  \n ', null, {}, 123]) assert.throws(() => logic.validateSpec({...input, prompt}));
});

test('image prompt whitespace is normalized before DOM matching', () => {
  const cleaned = logic.validateSpec({...input, prompt: '  A blue vase.\n '});
  assert.equal(cleaned.prompt, 'A blue vase.');
  const candidate = card(first, ['Seedream 5.0 Lite', '1:1', '2K'], 'A blue vase.');
  assert.equal(logic.selectNewRecord([candidate], [], cleaned).id, first);
});

test('image new-card selection handles concatenated text with metadata labels', () => {
  assert.equal(logic.selectNewRecord([card(first)], [], spec).id, first);
  assert.equal(logic.selectNewRecord([card(first)], [first], spec), null);
  assert.equal(logic.selectNewRecord([card(first), card(first)], [], spec).id, first);
});

test('image selection rejects other prompts, ratios, resolutions and model variants', () => {
  const variants = [
    card(first, ['Seedream 5.0', '1:1', '2K']),
    card(first, ['Seedream 5.0 Lite', '16:9', '2K']),
    card(first, ['Seedream 5.0 Lite', '1:1', '4K']),
    card(first, ['Seedream 5.0 Lite', '1:1', '2K'], spec.prompt + ' Another request'),
    card('not-a-uuid'),
  ];
  for (const value of variants) assert.equal(logic.selectNewRecord([value], [], spec), null);
});

test('an image card cannot satisfy a video request or vice versa', () => {
  const video = logic.validateSpec({prompt: spec.prompt});
  assert.equal(logic.selectNewRecord([card(first)], [], video), null);
  assert.equal(logic.selectNewRecord([card(first, [logic.MODEL_NAME, '4s', '16:9', '720P'])], [], spec), null);
});

test('ambiguous image cards stop rather than choosing the first', () => {
  assert.throws(() => logic.selectNewRecord([card(first), card(second)], [], spec), /多个/);
});

test('image job recovery never retries a lost click', () => {
  const inflight = {id: 'image-job', deadline: 100, phase: 'clicking'};
  assert.deepEqual(logic.recoveryAction(inflight, null, 10), {action: 'uncertain'});
  const result = {job_id: inflight.id, status: 'submitted', submit_id: first};
  assert.deepEqual(logic.recoveryAction({...inflight, phase: 'uncertain'}, {lastResult: result}, 200), {action: 'report', result});
});
