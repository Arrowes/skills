import assert from 'node:assert/strict';
import { handleBodyCoach, runPython } from './bridge_meals.mjs';

const sent = [];
const nonce = 'a'.repeat(32);
const ctx = { fromCardAction: true, msg: { chatId: 'test-chat', senderId: 'test-user', messageId: 'test-message' }, formValue: { breakfast_choice: '0' }, channel: { send: async (...args) => sent.push(args) } };
let calls = 0;
let release;
const invoke = event => {
  calls++;
  assert.equal(event.nonce, nonce);
  assert.deepEqual(event.form_value, ctx.formValue);
  return new Promise(resolve => { release = resolve; });
};
await handleBodyCoach(`meal ${nonce}`, { ...ctx, fromCardAction: false }, invoke);
await handleBodyCoach('invalid', ctx, invoke);
assert.equal(calls, 0);
await handleBodyCoach(`meal ${nonce}`, ctx, invoke);
await handleBodyCoach(`meal ${nonce}`, ctx, invoke);
assert.equal(calls, 1);
release({ text: 'test receipt' });
await new Promise(resolve => setImmediate(resolve));
assert.equal(sent[0][1].text, 'test receipt');
// Exercise the real native-to-Python pipe with a rejected event, never a food write.
const rejected = await runPython({ nonce, chat_id: 'test-chat', sender_id: 'test-user', form_value: {} });
assert.equal(rejected.ok, false);
assert.match(rejected.text, /指定会话/);
console.log('native meal adapter: routing, async acknowledgement, duplicate click, receipt, real Python rejection OK');
