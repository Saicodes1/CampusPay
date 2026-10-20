import test from "node:test";
import assert from "node:assert/strict";
import {cents, money, intentId, restorePending, description} from "../campuspay/web/utils.mjs";

test("amount parsing uses exact minor units", () => {
  assert.equal(cents("1.99"),199); assert.equal(cents("0.01"),1); assert.equal(cents("10000"),1000000);
  assert.equal(cents(" 25.5 "),2550); assert.equal(money(199),"$1.99");
});
test("invalid, fractional-cent and excessive amounts are rejected", () => {
  for (const raw of ["", "0", "-1", "1.001", "1e3", "Infinity", "10,00", "01", "10000.01", "999999999999999999999"]) assert.throws(() => cents(raw));
});
test("payment codes only accept complete UUID payloads", () => {
  const id="11111111-2222-4333-8444-555555555555";
  assert.equal(intentId(`campuspay:intent:${id}`),id); assert.equal(intentId(id),id);
  assert.throws(() => intentId("https://attacker.example/")); assert.throws(() => intentId("<script>"));
});
test("pending recovery is actor-scoped and uses only known relative API routes", () => {
  const plan={actor:"alice",path:"/api/funding",key:"11111111-2222-4333-8444-555555555555",body:{amount:100},label:"Added"};
  assert.deepEqual(restorePending(JSON.stringify(plan),"alice"),plan);
  assert.equal(restorePending(JSON.stringify(plan),"bob"),null);
  assert.equal(restorePending(JSON.stringify({...plan,path:"https://attacker.example"}),"alice"),null);
  assert.equal(restorePending(JSON.stringify({...plan,path:"/api/admin/replay/outbox/abc"}),"alice"),null);
  assert.equal(restorePending("invalid JSON","alice"),null);
});
test("activity descriptions reflect debit/credit direction", () => {
  assert.equal(description({kind:"TRANSFER",side:"DEBIT",counterparty:"Bob Student"}),"To Bob Student");
  assert.equal(description({kind:"TRANSFER",side:"CREDIT",counterparty:"Alice Student"}),"From Alice Student");
  assert.equal(description({kind:"CAPTURE",counterparty:"Campus Cafe"}),"Paid Campus Cafe");
});
