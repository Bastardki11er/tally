// Money crosses the API as an integer number of minor units: cents for USD, yen for
// JPY (no minor unit), fils for KWD (three digits). The console converts only at the
// edges, and from strings, so no amount ever passes through floating point.

const formatters = new Map();

function formatter(currency) {
  if (!formatters.has(currency)) {
    formatters.set(currency, new Intl.NumberFormat(undefined, { style: "currency", currency }));
  }
  return formatters.get(currency);
}

/** How many digits the currency's minor unit has: 2 for USD, 0 for JPY, 3 for KWD. */
export function minorDigits(currency) {
  return formatter(currency).resolvedOptions().maximumFractionDigits;
}

/** 1234 minor units of USD -> "12.34" (an exact decimal string, no currency symbol). */
export function toDecimal(minor, currency = "USD") {
  const digits = minorDigits(currency);
  const sign = minor < 0 ? "-" : "";
  const units = String(Math.abs(minor)).padStart(digits + 1, "0");
  return digits ? `${sign}${units.slice(0, -digits)}.${units.slice(-digits)}` : `${sign}${units}`;
}

/** 1234 minor units of USD -> "$12.34", formatted for the user's locale. */
export function formatMoney(minor, currency = "USD") {
  // Intl formats a decimal string exactly; dividing by 100 would go through a float.
  return formatter(currency).format(toDecimal(minor, currency));
}

/**
 * What the user typed -> minor units: "12.34" USD -> 1234, "500" JPY -> 500.
 * Refuses anything that isn't exact (a third decimal for USD, any decimal for JPY,
 * signs, exponents) instead of silently rounding someone's money.
 */
export function toMinor(text, currency = "USD") {
  const digits = minorDigits(currency);
  const match = /^(\d+)(?:\.(\d+))?$/.exec(String(text).trim());
  if (!match || (match[2] ?? "").length > digits) {
    const places = digits ? `at most ${digits} decimal place${digits > 1 ? "s" : ""}` : "no decimals";
    throw new RangeError(`"${text}" is not an amount of ${currency} (${places})`);
  }
  const minor = BigInt(match[1] + (match[2] ?? "").padEnd(digits, "0"));
  if (minor > BigInt(Number.MAX_SAFE_INTEGER)) throw new RangeError(`"${text}" is too large`);
  return Number(minor);
}
