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
