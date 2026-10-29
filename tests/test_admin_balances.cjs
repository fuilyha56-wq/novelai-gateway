// Run with: node --test tests/test_admin_balances.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../src/proxy/templates/admin.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function dashboard() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      innerHTML: '', textContent: '', style: {}, disabled: false, focus() {},
      classList: { contains: () => false },
    });
    return elements.get(id);
  };
  const context = vm.createContext({
    document: {
      getElementById: element, querySelectorAll: () => [],
      documentElement: { style: { setProperty() {}, removeProperty() {} } },
    },
    localStorage: { getItem: () => null },
    setInterval() {}, clearInterval() {}, setTimeout() {},
  });
  vm.runInContext(script, context);
  return { context, element };
}

const subscription = (percent, fixed = 1000, purchased = 200) => ({
  tier: 3, active: true, usage: { percent },
  trainingStepsLeft: { fixedTrainingStepsLeft: fixed, purchasedTrainingSteps: purchased },
});

test('adds remaining balances above 100% and refreshes each distinct credential once', async () => {
  const { context, element } = dashboard(), calls = [];
  context.api = async endpoint => {
    calls.push(endpoint);
    if (endpoint === '/accounts') return { accounts: [
      { id: 'a', credential_group: 'a', name: 'A' },
      { id: 'duplicate', credential_group: 'a', name: 'Duplicate' },
      { id: 'b', credential_group: 'b', name: 'B', enabled: false, allow_v5: false, allow_anlas: false },
    ] };
    return { subscription: subscription(endpoint.endsWith('=a') ? 80 : 70, '1000', '200') };
  };
  await context.loadUps();
  assert.equal(calls.length, 3);
  assert.match(element('balanceTotals').innerHTML, /2,400/);
  assert.match(element('balanceTotals').innerHTML, /150%/);
  assert.match(element('balanceTotals').innerHTML, /已同步 2\/2 个独立账号/);
  assert.equal(element('upGo').disabled, false);
});

test('marks failed accounts as incomplete without discarding successful balances', async () => {
  const { context, element } = dashboard();
  context.api = async endpoint => {
    if (endpoint === '/accounts') return { accounts: [{ id: 'a' }, { id: 'b' }] };
    if (endpoint.endsWith('=b')) throw new Error('Upstream unavailable');
    return { subscription: subscription(65) };
  };
  await context.loadUps();
  assert.match(element('balanceTotals').innerHTML, /1,200/);
  assert.match(element('balanceTotals').innerHTML, /65%/);
  assert.match(element('balanceTotals').innerHTML, /已同步 1\/2 个独立账号/);
  assert.match(element('balanceTotals').innerHTML, /部分数据缺失/);
  assert.match(element('upT').textContent, /部分账号拉取失败/);
});

test('unknown quotas remain unknown and exhausted quotas never reduce other accounts', () => {
  const { context, element } = dashboard();
  for (const percent of [null, undefined, '', ' ', NaN, Infinity, false]) {
    assert.equal(context.accountBalances(subscription(percent)).v5, null);
  }
  const debt = subscription(10); debt.usage.isNegative = true;
  const expired = subscription(100); expired.active = false;
  const nonOpus = subscription(100); nonOpus.tier = 2;
  context.renderBalanceTotals([
    { subscription: subscription(150.25) }, { subscription: debt },
    { subscription: subscription(-5) }, { subscription: expired }, { subscription: nonOpus },
  ], 5);
  assert.match(element('balanceTotals').innerHTML, /150\.25%/);
  context.renderBalanceTotals([{ subscription: {} }], 1);
  assert.match(element('balanceTotals').innerHTML, /已同步 0\/1 个独立账号/);
  assert.doesNotMatch(element('balanceTotals').innerHTML, /class="big">0%/);
});

test('an empty account pool displays zero and a failed list request clears stale totals', async () => {
  const { context, element } = dashboard();
  context.api = async () => ({ accounts: [] });
  await context.loadUps();
  assert.match(element('balanceTotals').innerHTML, /class="big">0%/);
  context.api = async () => { throw new Error('offline'); };
  await assert.rejects(context.loadUps(), /offline/);
  assert.match(element('balanceTotals').innerHTML, /额度同步失败/);
  assert.equal(element('upGo').disabled, false);
});
