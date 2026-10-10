// Run: node --test 'tests/js/*.test.js'   (Node 22+, no dependencies)
import assert from "node:assert/strict";
import { test } from "node:test";

import { formatMoney, minorDigits, toDecimal, toMinor } from "../../src/tally/static/money.js";

test("each currency has its own number of minor digits", () => {
  assert.equal(minorDigits("USD"), 2);
  assert.equal(minorDigits("JPY"), 0);
  assert.equal(minorDigits("KWD"), 3);
});

test("typed amounts become exact minor units, without float rounding", () => {
  assert.equal(toMinor("12.34"), 1234);
  assert.equal(toMinor("0.29"), 29); // 0.29 * 100 is 28.999999999999996 in floating point
  assert.equal(toMinor("1.1"), 110);
  assert.equal(toMinor(" 7 "), 700);
  assert.equal(toMinor("500", "JPY"), 500);
  assert.equal(toMinor("1.234", "KWD"), 1234);
  assert.equal(toMinor("90071992547409.91"), 9007199254740991); // the largest safe integer
});

test("anything inexact is refused rather than rounded", () => {
  for (const text of ["1.005", "-5", "1e3", "", "abc", "1.2.3", ".5", "12,50"]) {
    assert.throws(() => toMinor(text), RangeError, text);
  }
  assert.throws(() => toMinor("5.5", "JPY"), /no decimals/);
  assert.throws(() => toMinor("90071992547409.92"), /too large/);
});

test("minor units format back exactly", () => {
  assert.equal(toDecimal(1234), "12.34");
  assert.equal(toDecimal(5), "0.05");
  assert.equal(toDecimal(-5), "-0.05");
  assert.equal(toDecimal(500, "JPY"), "500");
  assert.equal(toDecimal(1234, "KWD"), "1.234");
  assert.equal(toDecimal(9007199254740991), "90071992547409.91");
  for (const text of ["0.01", "19.99", "1000000.00"]) assert.equal(toDecimal(toMinor(text)), text);
});

test("formatting uses the currency's own minor unit", () => {
  // The old console divided every currency by 100, showing 500 yen as ¥5.
  const plain = (s) => s.replace(/\s/g, " ");
  assert.match(plain(formatMoney(500, "JPY")), /500/);
  assert.doesNotMatch(formatMoney(500, "JPY"), /5\.00|\b5\b/);
  assert.match(formatMoney(1234, "USD"), /12\.34/);
  assert.match(formatMoney(1234, "KWD"), /1\.234/);
});
