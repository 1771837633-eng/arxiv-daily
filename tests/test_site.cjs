const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function page() {
  const nodes = new Map();
  const context = vm.createContext({
    document: {getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, {innerHTML: '', addEventListener() {}});
      return nodes.get(id);
    }},
    fetch: () => new Promise(() => {}), window: {}, Intl, Date,
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../site/app.js'), 'utf8'), context);
  vm.runInContext(`state.edition = '2026-10-12'; state.data = {papers: [{arxiv_id: '1v1', published: '2026-10-12', title: '<script>unsafe</script>'}], daily_briefs: {}};`, context);
  return {context, nodes};
}

test('old datasets render an honest empty brief, all-dates hides it', () => {
  const {context, nodes} = page();
  vm.runInContext('renderDailyBrief()', context);
  assert.match(nodes.get('daily-brief').innerHTML, /尚未生成/);
  vm.runInContext("state.edition = 'all'; renderDailyBrief()", context);
  assert.equal(nodes.get('daily-brief').hidden, true);
});

test('brief displays coverage and escapes text and reference titles', () => {
  const {context, nodes} = page();
  vm.runInContext(`state.data.daily_briefs['2026-10-12'] = {status:'ready', covered:1, total:2, headline:'<b>headline</b>', overview:'Overview', caveat:'Abstract only', themes:[{title:'Theme',summary:'Finding',paper_ids:['1v1','unknown']}]}; renderDailyBrief();`, context);
  const html = nodes.get('daily-brief').innerHTML;
  assert.match(html, /1 \/ 2/);
  assert.match(html, /&lt;b&gt;headline/);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>|data-brief-paper="unknown"/);
  assert.match(html, /#paper-1v1/);
});

test('English mode never labels original abstract as an English AI guide', () => {
  const {context, nodes} = page();
  vm.runInContext("state.language='en'; state.data.papers[0].summary_mode='llm-test'; renderPapers(state.data.papers)", context);
  assert.match(nodes.get('paper-list').innerHTML, /English guide pending/);
  assert.match(nodes.get('paper-list').innerHTML, /corresponding author not confirmed/);
});

test('bilingual rendering includes first affiliation and last verified correspondent', () => {
  const {context, nodes} = page();
  vm.runInContext(`state.language='both'; Object.assign(state.data.papers[0], {authors:['First','Middle','Last'],first_affiliation:'Test Institute',summary_mode:'llm-test',abstract_summary_zh:'Chinese finding',english_guide:{finding:'English finding'},author_metadata:{corresponding_authors:['Middle','Last']}}); renderPapers(state.data.papers);`, context);
  const html = nodes.get('paper-list').innerHTML;
  assert.match(html, /第一作者：First/);
  assert.match(html, /末位通讯：Last/);
  assert.match(html, /Test Institute/);
  assert.match(html, /Chinese finding/);
  assert.match(html, /English finding/);
});
