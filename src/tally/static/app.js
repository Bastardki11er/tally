// Tally ledger console: a thin client over the public REST API. No build step, no dependencies.

import { ApiError, createActions } from "./actions.js";
import { formatMoney as money, toDecimal, toMinor } from "./money.js";

const $ = (selector, root = document) => root.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const when = (iso) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "medium" });

const REFRESH_EVERY_MS = 4_000;
// A reconciliation re-derives the whole ledger from raw entries: about 1.5 s for
// 1.4M entries. Every 4 s, one open tab would keep the database busy a third of the time.
const RECONCILE_EVERY_MS = 60_000;
const ACCOUNT_PAGE = 200;

const state = {
  accounts: [], // the loaded pages, plus accounts created here that aren't on them
  accountPages: 1,
  moreAccounts: false,
  pinned: new Set(),
  names: new Map(), // id -> account, for every account seen; names label the feed
  lastRequest: null,
  rendered: {}, // section -> JSON of what it shows, so identical data isn't re-rendered
};
const accountName = (id) => state.names.get(id)?.name ?? `#${id}`;
const currencyOf = (id) => state.names.get(id)?.currency ?? "USD";
const remember = (accounts) => accounts.forEach((a) => state.names.set(a.id, a));

/** True if ``data`` differs from what ``section`` last rendered. */
function changed(section, data) {
  const json = JSON.stringify(data);
  if (state.rendered[section] === json) return false;
  state.rendered[section] = json;
  return true;
}

/** At most one run at a time; calls made meanwhile collapse into one more run afterwards. */
function serialized(fn) {
  let running = null;
  let again = false;
  return () => {
    if (running) {
      again = true;
      return running;
    }
    running = (async () => {
      try {
        do {
          again = false;
          await fn();
        } while (again);
      } finally {
        running = null;
      }
    })();
    return running;
  };
}

// ---- API ------------------------------------------------------------------

async function api(method, path, body, idempotencyKey) {
  const headers = body ? { "Content-Type": "application/json" } : {};
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  let response, text;
  try {
    response = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
    text = await response.text();
  } catch {
    throw new ApiError(0, { code: "NETWORK_ERROR", detail: "Could not reach the ledger" });
  }
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (idempotencyKey) {
    state.lastRequest = { method, path, body, idempotencyKey, status: response.status, replayed: response.headers.get("Idempotent-Replayed"), code: data?.code };
    renderLastRequest();
  }
  if (!response.ok) throw new ApiError(response.status, data);
  return data;
}

// Every action that changes the ledger goes through here: one key per user intent.
const act = createActions(api);

function toast(message, kind = "") {
  const node = Object.assign(document.createElement("div"), { className: `toast ${kind}`, textContent: message });
  $("#toasts").append(node);
  setTimeout(() => node.remove(), 4500);
}

const FRIENDLY = {
  IDEMPOTENCY_KEY_REUSED: "An earlier attempt of this action already went through. Check the ledger before trying again.",
};
const fail = (error) =>
  toast(FRIENDLY[error.code] ?? (error.code ? `${error.code}: ${error.message}` : error.message), "bad");

// ---- Reconciliation badge -------------------------------------------------

const checkBooks = serialized(async () => {
  const badge = $("#recon");
  badge.classList.add("checking");
  const started = performance.now();
  try {
    const report = await api("GET", "/v1/reconciliation");
    const seconds = ((performance.now() - started) / 1000).toFixed(1);
    const at = new Date().toLocaleTimeString(undefined, { timeStyle: "short" });
    const totals = report.trial_balance.map((t) => `${money(t.debits, t.currency)} debits = credits`).join(" · ");
    const problems = Object.values(report.totals).reduce((sum, n) => sum + n, 0);
    badge.className = `recon ${report.ok ? "ok" : "bad"}`;
    badge.textContent = report.ok
      ? `✓ Books balanced${totals ? ` · ${totals}` : ""} · ${at}`
      : `✗ ${problems.toLocaleString()} discrepancies · ${at}`;
    badge.title = `Every balance re-derived from the raw entries at ${at} in ${seconds} s. Click to check again.`;
  } catch {
    badge.className = "recon bad";
    badge.textContent = "Couldn't check the books";
  }
});

$("#recon").onclick = checkBooks;

// ---- Accounts -------------------------------------------------------------

async function loadAccounts() {
  const accounts = [];
  let cursor = null;
  for (let page = 0; page < state.accountPages; page++) {
    const query = new URLSearchParams({ limit: ACCOUNT_PAGE, ...(cursor && { cursor }) });
    const result = await api("GET", `/v1/accounts?${query}`);
    accounts.push(...result.data);
    cursor = result.next_cursor;
    if (!cursor) break;
  }
  // Accounts are listed oldest first, so one created here may be pages away.
  const loaded = new Set(accounts.map((a) => a.id));
  const pinned = [...state.pinned].filter((id) => !loaded.has(id));
  accounts.push(...(await Promise.all(pinned.map((id) => api("GET", `/v1/accounts/${id}`)))));
  state.accounts = accounts;
  state.moreAccounts = Boolean(cursor);
  remember(accounts);
  renderAccounts();
  fillAccountSelects();
}

function renderAccounts() {
  if (!changed("accounts", [state.accounts, state.moreAccounts])) return;
  const box = $("#accounts");
  if (!state.accounts.length) {
    box.innerHTML = `<div class="empty">No accounts yet. Run <code>tally demo</code> or create one on the right.</div>`;
    return;
  }
  box.innerHTML = `<table>
    <thead><tr><th>Account</th><th>Type</th><th class="num">Balance</th><th class="num">Held</th><th class="num">Available</th></tr></thead>
    <tbody>${state.accounts
      .map(
        (a) => `<tr class="clickable" data-id="${a.id}" tabindex="0">
          <td><strong>${esc(a.name)}</strong> <span class="muted small">#${a.id}</span></td>
          <td><span class="chip">${esc(a.type)}</span> <span class="muted small">${esc(a.normal_side)}-normal</span></td>
          <td class="money">${money(a.balance, a.currency)}</td>
          <td class="money muted">${a.held ? money(a.held, a.currency) : "–"}</td>
          <td class="money">${money(a.available, a.currency)}</td></tr>`,
      )
      .join("")}</tbody></table>
    ${state.moreAccounts ? `<div class="row end"><button id="more-accounts" class="small">Show ${ACCOUNT_PAGE} more accounts</button></div>` : ""}`;
}

// One listener for the whole list, so rows keep working across re-renders.
$("#accounts").addEventListener("click", (event) => {
  if (event.target.closest("#more-accounts")) {
    state.accountPages += 1;
    refresh();
    return;
  }
  const row = event.target.closest("tr[data-id]");
  if (row) openAccount(Number(row.dataset.id));
});
$("#accounts").addEventListener("keydown", (event) => {
  const row = event.target.closest("tr[data-id]");
  if (row && (event.key === "Enter" || event.key === " ")) {
    event.preventDefault();
    openAccount(Number(row.dataset.id));
  }
});

const accountOptions = (accounts) =>
  accounts.map((a) => `<option value="${a.id}">${esc(a.name)} (${esc(a.currency)})</option>`).join("");

function fillAccountSelects() {
  // Only when the list itself changes: rebuilding a select closes it under the user's cursor.
  if (!changed("selects", state.accounts.map((a) => [a.id, a.name, a.currency]))) return;
  const options = accountOptions(state.accounts);
  document.querySelectorAll(".account-select").forEach((select) => {
    const current = select.value;
    select.innerHTML = options;
    if (current) select.value = current;
  });
  const transfer = $("#form-transfer");
  if (!transfer.dataset.init && state.accounts.length > 1) {
    transfer.debit.value = state.accounts[Math.min(2, state.accounts.length - 1)].id;
    transfer.credit.value = state.accounts[Math.min(3, state.accounts.length - 1)].id;
    transfer.dataset.init = "1";
  }
  if (!$("#journal-lines").children.length && state.accounts.length) resetJournal();
}

// ---- Transactions feed ----------------------------------------------------

async function resolveNames(ids) {
  const missing = [...new Set(ids)].filter((id) => !state.names.has(id));
  const found = await Promise.all(missing.map((id) => api("GET", `/v1/accounts/${id}`).catch(() => null)));
  remember(found.filter(Boolean));
}

async function loadTransactions() {
  const feed = (await api("GET", "/v1/transactions?limit=15")).data;
  // The feed can involve accounts beyond the loaded pages; fetch their names once.
  await resolveNames(feed.flatMap((t) => t.entries.map((e) => e.account_id)));
  if (!changed("transactions", feed)) return;
  const box = $("#transactions");
  if (!feed.length) {
    box.innerHTML = `<div class="empty">No transactions yet.</div>`;
    return;
  }
  box.innerHTML = feed
    .map((t) => {
      const lines = t.entries
        .map(
          (e) => `<span class="acct ${e.direction === "credit" ? "cr-line" : ""}">${esc(accountName(e.account_id))}</span>
            <span class="num dr">${e.direction === "debit" ? money(e.amount, e.currency) : ""}</span>
            <span class="num cr">${e.direction === "credit" ? money(e.amount, e.currency) : ""}</span>`,
        )
        .join("");
      const badges = `${t.reverses_id ? '<span class="badge reversal">reversal</span>' : ""}${t.reversed_by ? '<span class="badge reversed">reversed</span>' : ""}`;
      const canReverse = !t.reverses_id && !t.reversed_by;
      return `<div class="txn">
        <div class="txn-head"><span class="desc">${esc(t.description || "(no description)")}</span>${badges}
          <span class="muted small">${when(t.posted_at)}</span>
          ${canReverse ? `<button class="small danger" data-reverse="${esc(t.id)}">Reverse</button>` : ""}</div>
        <div class="txn-lines"><span class="muted small">Account</span><span class="muted small num">Debit</span><span class="muted small num">Credit</span>${lines}</div>
      </div>`;
    })
    .join("");
}

$("#transactions").addEventListener("click", (event) => {
  const button = event.target.closest("[data-reverse]");
  if (!button) return;
  const id = button.dataset.reverse;
  press(button, `reverse:${id}`, ["POST", `/v1/transactions/${id}/reversal`, { description: "Reversed from the console" }], () => "Reversed: a mirror-image transaction was posted");
});

// ---- Writes ---------------------------------------------------------------

function afterWrite() {
  refresh();
  checkBooks();
}

/** Runs a ledger action for a button, disabling it until the server answers. */
async function press(button, scope, request, done) {
  if (button.disabled) return undefined; // a second submit (Enter, double click) while the first runs
  button.disabled = true;
  try {
    const result = await act(scope, ...request);
    if (result === undefined) return undefined; // the same action is already in flight
    toast(done(result));
    afterWrite();
    return result;
  } catch (error) {
    fail(error);
    return undefined;
  } finally {
    button.disabled = false;
  }
}

/** The same for a form. ``build`` reads the form and may refuse it (bad amount, say). */
function onSubmit(form, scope, build, done) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    let request;
    try {
      request = build(form);
    } catch (error) {
      fail(error);
      return;
    }
    await press($("[type=submit]", form), scope, request, done);
  });
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.onclick = () => {
    document.querySelectorAll(".tab").forEach((t) => {
      t.classList.toggle("active", t === tab);
      t.setAttribute("aria-selected", String(t === tab));
    });
    document.querySelectorAll(".tab-panel").forEach((p) => (p.hidden = p.id !== `form-${tab.dataset.tab}`));
  };
});

onSubmit(
  $("#form-transfer"),
  "transfer",
  (f) => {
    const debit = Number(f.debit.value);
    const body = {
      debit_account_id: debit,
      credit_account_id: Number(f.credit.value),
      amount: toMinor(f.amount.value, currencyOf(debit)),
      description: f.description.value,
    };
    return ["POST", "/v1/transfers", body];
  },
  () => "Transfer posted",
);

function journalLine(accountId = state.accounts[0]?.id, debit = "", credit = "") {
  const row = document.createElement("tr");
  row.innerHTML = `<td><select class="account-select" aria-label="Account"></select></td>
    <td><input inputmode="decimal" placeholder="0.00" value="${esc(debit)}" data-side="debit" aria-label="Debit"></td>
    <td><input inputmode="decimal" placeholder="0.00" value="${esc(credit)}" data-side="credit" aria-label="Credit"></td>
    <td><button type="button" class="small" aria-label="Remove line">×</button></td>`;
  $("select", row).innerHTML = accountOptions(state.accounts);
  if (accountId) $("select", row).value = accountId;
  $("button", row).onclick = () => {
    row.remove();
    updateJournalTotals();
  };
  row.querySelectorAll("input, select").forEach((input) => (input.oninput = updateJournalTotals));
  return row;
}

function resetJournal() {
  const body = $("#journal-lines");
  body.innerHTML = "";
  const [a, b, c] = state.accounts.slice(2, 5).map((acct) => acct.id);
  body.append(journalLine(a, "30.90"), journalLine(b, "", "30.00"), journalLine(c ?? b, "", "0.90"));
  updateJournalTotals();
}

/** The journal's lines as API entries. Throws RangeError if an amount isn't exact. */
function journalEntries() {
  return [...$("#journal-lines").children].flatMap((row) => {
    const accountId = Number($("select", row).value);
    return [...row.querySelectorAll("input")]
      .filter((input) => input.value.trim() !== "")
      .map((input) => ({ account_id: accountId, direction: input.dataset.side, amount: toMinor(input.value, currencyOf(accountId)) }))
      .filter((entry) => entry.amount > 0);
  });
}

function updateJournalTotals() {
  const check = $("#journal-balance");
  let entries;
  try {
    entries = journalEntries();
  } catch (error) {
    check.className = "balance-check bad";
    check.textContent = `✗ ${error.message}`;
    return;
  }
  // Each currency must balance on its own, as the API and the database require.
  const totals = new Map();
  for (const e of entries) {
    const currency = currencyOf(e.account_id);
    const t = totals.get(currency) ?? { debit: 0, credit: 0 };
    t[e.direction] += e.amount;
    totals.set(currency, t);
  }
  const show = (side) => [...totals].map(([c, t]) => money(t[side], c)).join(" + ") || money(0);
  $("#total-debit").textContent = show("debit");
  $("#total-credit").textContent = show("credit");
  const off = [...totals].filter(([, t]) => t.debit !== t.credit);
  const balanced = totals.size > 0 && off.length === 0;
  check.className = `balance-check ${balanced ? "ok" : "bad"}`;
  check.textContent = balanced
    ? "✓ Balanced: debits equal credits"
    : off.length
      ? `✗ Out of balance by ${off.map(([c, t]) => money(Math.abs(t.debit - t.credit), c)).join(" and ")}`
      : "✗ Enter at least one debit and one credit";
}

$("#add-line").onclick = () => $("#journal-lines").append(journalLine());

onSubmit(
  $("#form-journal"),
  "journal",
  (f) => ["POST", "/v1/transactions", { entries: journalEntries(), description: f.description.value }],
  () => "Journal entry posted",
);

onSubmit(
  $("#form-hold"),
  "hold",
  (f) => {
    const account = Number(f.account.value);
    const body = {
      account_id: account,
      amount: toMinor(f.amount.value, currencyOf(account)),
      expires_in_seconds: Number(f.expires.value),
      description: f.description.value,
    };
    return ["POST", "/v1/holds", body];
  },
  () => "Hold placed: available balance reduced",
);

onSubmit(
  $("#form-account"),
  "account",
  (f) => ["POST", "/v1/accounts", { name: f.name.value, type: f.type.value, currency: f.currency.value.toUpperCase(), allow_negative: f.allow_negative.checked }],
  (account) => {
    state.pinned.add(account.id); // keep it in view even if it lands past the loaded pages
    $("#form-account").reset();
    return `Created ${account.name} (#${account.id})`;
  },
);

// ---- Last request / idempotent replay -------------------------------------

function renderLastRequest() {
  const r = state.lastRequest;
  if (!r) return;
  const ok = r.status < 300;
  $("#last-request").innerHTML = `
    <div class="status-line">
      <span class="chip ${ok ? "captured" : "expired"}">${r.status}${r.code ? ` ${esc(r.code)}` : ""}</span>
      ${r.replayed === "true" ? '<span class="chip active">replayed: nothing moved</span>' : ""}
    </div>
    <dl class="kv">
      <dt>Request</dt><dd><code>${esc(r.method)} ${esc(r.path)}</code></dd>
      <dt>Idempotency-Key</dt><dd><code>${esc(r.idempotencyKey)}</code></dd>
    </dl>
    <div class="row end"><button id="replay" class="small">Send again with the same key</button></div>`;
  $("#replay").onclick = async () => {
    try {
      // Deliberately the same key: the demo of a retry that moves nothing.
      await api(r.method, r.path, r.body, r.idempotencyKey);
      toast(state.lastRequest.replayed === "true" ? "Replayed: same response, no money moved" : "Sent");
      afterWrite();
    } catch (error) {
      fail(error);
    }
  };
}

// ---- Account detail -------------------------------------------------------

const detail = { id: null, currency: "USD", cursor: null };

const statementRows = (entries, c) =>
  entries
    .map(
      (e) => `<tr><td class="small">${when(e.posted_at)}</td><td>${esc(e.description || "–")}</td>
        <td class="money dr">${e.direction === "debit" ? money(e.amount, c) : ""}</td>
        <td class="money cr">${e.direction === "credit" ? money(e.amount, c) : ""}</td>
        <td class="money">${money(e.balance_after, c)}</td></tr>`,
    )
    .join("");

async function openAccount(id) {
  try {
    await renderAccountDetail(id);
    if (!$("#account-dialog").open) $("#account-dialog").showModal();
  } catch (error) {
    fail(error);
  }
}

async function renderAccountDetail(id) {
  const [account, statement, holds] = await Promise.all([
    api("GET", `/v1/accounts/${id}`),
    api("GET", `/v1/accounts/${id}/entries?limit=20`),
    api("GET", `/v1/accounts/${id}/holds`),
  ]);
  const c = account.currency;
  Object.assign(detail, { id, currency: c, cursor: statement.next_cursor });
  $("#account-detail").innerHTML = `
    <h2>${esc(account.name)} <span class="muted small">#${account.id} · ${esc(account.type)}, ${esc(account.normal_side)}-normal · ${esc(c)}</span></h2>
    <div class="summary">
      <div class="stat"><div class="label">Balance</div><div class="value">${money(account.balance, c)}</div></div>
      <div class="stat"><div class="label">Held</div><div class="value">${money(account.held, c)}</div></div>
      <div class="stat"><div class="label">Available</div><div class="value">${money(account.available, c)}</div></div>
    </div>
    <div class="row">
      <label>Balance as of <input type="datetime-local" step="1" id="as-of"></label>
      <label>Result <input id="as-of-result" readonly placeholder="pick a moment"></label>
    </div>
    <h3>Holds</h3>
    ${holds.data.length ? `<table><thead><tr><th>Created</th><th>Description</th><th class="num">Amount</th><th>Status</th><th></th></tr></thead><tbody>
      ${holds.data.map((h) => `<tr><td class="small">${when(h.created_at)}</td><td>${esc(h.description || "–")}</td>
        <td class="money">${money(h.amount, c)}</td><td><span class="chip ${esc(h.status)}">${esc(h.status)}</span></td>
        <td>${h.status === "active" ? `<button class="small" data-capture="${esc(h.id)}" data-amount="${h.amount}">Capture</button> <button class="small danger" data-void="${esc(h.id)}">Void</button>` : ""}</td></tr>`).join("")}
      </tbody></table>${holds.next_cursor ? '<p class="muted small">Showing the latest 50 holds.</p>' : ""}` : '<div class="empty">No holds on this account.</div>'}
    <h3>Statement</h3>
    ${statement.data.length ? `<table><thead><tr><th>Posted</th><th>Description</th><th class="num">Debit</th><th class="num">Credit</th><th class="num">Balance</th></tr></thead>
      <tbody id="statement-rows">${statementRows(statement.data, c)}</tbody></table>
      ${statement.next_cursor ? '<div class="row end"><button id="load-older" class="small">Load older entries</button></div>' : ""}` : '<div class="empty">No entries yet.</div>'}`;
}

const detailBox = $("#account-detail");

detailBox.addEventListener("change", async (event) => {
  if (event.target.id !== "as-of" || !event.target.value) return;
  try {
    // datetime-local is the user's local time; toISOString() sends it with a zone (Z).
    const asOf = new Date(event.target.value).toISOString();
    const result = await api("GET", `/v1/accounts/${detail.id}/balance?as_of=${encodeURIComponent(asOf)}`);
    $("#as-of-result").value = money(result.balance, detail.currency);
  } catch (error) {
    fail(error);
  }
});

detailBox.addEventListener("click", async (event) => {
  const older = event.target.closest("#load-older");
  if (older) {
    older.disabled = true;
    try {
      const page = await api("GET", `/v1/accounts/${detail.id}/entries?limit=20&cursor=${encodeURIComponent(detail.cursor)}`);
      $("#statement-rows").insertAdjacentHTML("beforeend", statementRows(page.data, detail.currency));
      detail.cursor = page.next_cursor;
      if (!detail.cursor) older.remove();
    } catch (error) {
      fail(error);
    } finally {
      older.disabled = false;
    }
    return;
  }

  const capture = event.target.closest("[data-capture]");
  if (capture) {
    detailBox.querySelectorAll(".capture-row").forEach((row) => row.remove());
    const holdId = capture.dataset.capture;
    // The capture posts a transaction, so the destination must share the hold's currency.
    const destinations = state.accounts.filter((a) => a.id !== detail.id && a.currency === detail.currency);
    capture.closest("tr").insertAdjacentHTML(
      "afterend",
      `<tr class="capture-row"><td colspan="5"><form class="inline" data-hold="${esc(holdId)}">
        <label>Pay to <select name="destination">${accountOptions(destinations)}</select></label>
        <label>Amount <input name="amount" inputmode="decimal" required value="${toDecimal(Number(capture.dataset.amount), detail.currency)}"></label>
        <button class="primary small" type="submit">Capture</button>
      </form></td></tr>`,
    );
    return;
  }

  const voidButton = event.target.closest("[data-void]");
  if (voidButton) {
    const holdId = voidButton.dataset.void;
    if (await press(voidButton, `void:${holdId}`, ["POST", `/v1/holds/${holdId}/void`], () => "Hold voided: funds available again")) {
      openAccount(detail.id);
    }
  }
});

detailBox.addEventListener("submit", async (event) => {
  const form = event.target.closest("form[data-hold]");
  if (!form) return;
  event.preventDefault();
  const holdId = form.dataset.hold;
  let body;
  try {
    body = { destination_account_id: Number(form.destination.value), amount: toMinor(form.amount.value, detail.currency) };
  } catch (error) {
    fail(error);
    return;
  }
  const done = () => "Captured: posted the transaction and released the rest of the hold";
  if (await press($("[type=submit]", form), `capture:${holdId}`, ["POST", `/v1/holds/${holdId}/capture`, body], done)) {
    openAccount(detail.id);
  }
});

$("#close-dialog").onclick = () => $("#account-dialog").close();

// ---- Refresh --------------------------------------------------------------

const refresh = serialized(async () => {
  try {
    await loadAccounts();
    await loadTransactions();
    if (!$("#offline").hidden) checkBooks(); // back online: don't wait a minute to re-check
    $("#offline").hidden = true;
  } catch (error) {
    // A banner, not a toast: polling would otherwise stack a new toast every few seconds.
    $("#offline").textContent = `Can't load the ledger (${error.message}). Retrying every ${REFRESH_EVERY_MS / 1000} s.`;
    $("#offline").hidden = false;
  }
});

const visible = () => !document.hidden;
setInterval(() => visible() && !$("#account-dialog").open && refresh(), REFRESH_EVERY_MS);
setInterval(() => visible() && checkBooks(), RECONCILE_EVERY_MS);
refresh();
checkBooks();
