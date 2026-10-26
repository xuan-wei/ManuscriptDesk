// Dependency-free behavioral tests for the single-state journey editor.
// Run: node tests/journey_editor.test.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.value = ''; this.hidden = false; this.disabled = false; this.textContent = ''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(key, value) { this[key] = value; }
  focus() { this.focused = true; }
}
const item = (id, date, title = id) => ({original_id: id, title, journal: 'Synthetic Journal', manuscript_id: id, status: 'Accept', submitted_date: date, submission_sort_date: date ? `${date}T00:00:00.000000+00:00` : ''});
const members = ['1', '2', '3', '4'].map((id, i) => item(id, `2024-0${i + 1}-01`, i === 0 ? 'Old searchable title' : id));
const data = {journeys: [{id: 1, revision: 2, member_count: 4, members, representative: members[3], hidden: true}], candidates: [item('5', '2024-05-01', '<script>different title</script>'), item('0', '', 'Unknown date')], selected_id: null};
const elements = new Map();
const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
get('journey-data').textContent = JSON.stringify(data);
get('journey-visibility').value = 'all';
const confirms = [], locations = [], requests = [];
let confirmation = true, response = {ok: true, status: 200, json: async () => ({journey: {id: 1}})};
const context = {document: {getElementById: get, createElement: tag => new Element(tag)}, window: {confirm: text => {confirms.push(text); return confirmation;}, addEventListener() {}, location: {assign: url => locations.push(url)}}, fetch: async (url, options) => { requests.push({url, options}); return response; }, console};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../src/manuscript_desk/static/ui/journeys.js'), 'utf8'), context);
const el = name => get(`journey-${name}`);
const clickAdd = () => el('list').children[0].children[2].children[0].onclick();
const checkbox = id => el('candidates').children.find(label => label.children[0]?.value === id)?.children[0];
const toggle = id => { const input = checkbox(id); input.checked = !input.checked; input.onchange(); };
(async () => {
  el('search').value = 'old searchable'; el('search').oninput();
  assert.equal(el('list').children.length, 1);
  clickAdd();
  assert.equal(el('members').children.length, 4);
  assert.equal(el('candidate-search').focused, true);
  el('candidate-search').value = 'different'; el('candidate-search').oninput();
  toggle('5');
  assert.equal(el('members').children.length, 5);
  assert.match(el('preview').textContent, /<script>different title/); // textContent, not HTML
  el('members').children[4].children[2].onclick();
  assert.equal(checkbox('5').checked, false);
  toggle('5');
  el('candidate-search').value = ''; el('candidate-search').oninput();
  toggle('0');
  assert.match(el('members').children[0].children[0].textContent, /Unknown date/);
  assert.match(el('preview').textContent, /different title/);
  confirmation = false; el('new').onclick(); assert.equal(el('members').children.length, 6);
  confirmation = true; el('cancel').onclick(); clickAdd();
  assert.equal(el('members').children.length, 4);
  assert.equal(checkbox('5').checked, false);
  toggle('5');
  response = {ok: false, status: 409, json: async () => ({detail: 'conflict'})};
  await el('save').onclick();
  assert.equal(el('reload').hidden, false);
  assert.equal(el('members').children.length, 5);
  assert.equal(el('save').disabled, false);
  el('reload').onclick(); assert.equal(locations.at(-1), '/journeys?edit=1');
  response = {ok: false, status: 500, json: async () => ({detail: 'retry'})};
  await el('save').onclick(); assert.equal(el('members').children.length, 5); assert.equal(el('feedback').textContent, 'retry');
  let release;
  context.fetch = async (url, options) => {requests.push({url, options}); return new Promise(resolve => {release = resolve;});};
  const pending = el('save').onclick();
  assert.equal(el('save').disabled, true);
  const count = requests.length; await el('save').onclick(); assert.equal(requests.length, count);
  release({ok: true, status: 200, json: async () => ({journey: {id: 1}})}); await pending;
  const saved = JSON.parse(requests.at(-1).options.body);
  assert.deepEqual(saved.ordered_original_ids, ['1', '2', '3', '4', '5']);
  assert.equal(saved.expected_revision, 2);
  clickAdd();
  while (el('members').children.length > 1) el('members').children[0].children[2].onclick();
  assert.equal(el('save').disabled, true);
  context.fetch = async (url, options) => {requests.push({url, options}); return {ok: true, json: async () => ({})};};
  await el('dissolve').onclick(); assert.equal(requests.at(-1).options.method, 'DELETE');
  assert.ok(confirms.some(text => text.includes('原隐藏/标星状态保留')));
  el('new').onclick(); assert.equal(el('members').children.length, 0); assert.equal(el('dissolve').hidden, true);
  console.log('Journey editor: search, append, unknown ordering, selection sync, cancel/reset, failure preservation, revision conflict/reload, duplicate save guard, minimum and dissolve PASS');
})().catch(error => {console.error(error); process.exitCode = 1;});
