// Tally ledger console: a thin client over the public REST API. No build step, no dependencies.

const $ = (selector, root = document) => root.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const money = (cents, currency = "USD") =>
  (cents / 100).toLocaleString(undefined, { style: "currency", currency });
const toCents = (dollars) => Math.round(Number(dollars) * 100);
const when = (iso) => new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "medium" });

const state = { accounts: [], lastRequest: null };
const accountName = (id) => state.accounts.find((a) => a.id === id)?.name ?? `#${id}`;
const currencyOf = (id) => state.accounts.find((a) => a.id === id)?.currency ?? "USD";

// ---- API ------------------------------------------------------------------

class ApiError extends Error {
  constructor(status, problem) {
    super(problem?.detail || `HTTP ${status}`);
    this.status = status;
    this.code = problem?.code;
  }
}

async function api(method, path, body, { idempotencyKey } = {}) {
  const headers = body ? { "Content-Type": "application/json" } : {};
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  const response = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  const text = await response.text();
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

// Money-moving requests get a fresh Idempotency-Key per user action.
const send = (method, path, body) => api(method, path, body, { idempotencyKey: crypto.randomUUID() });

function toast(message, kind = "") {
  const node = Object.assign(document.createElement("div"), { className: `toast ${kind}`, textContent: message });
  $("#toasts").append(node);
  setTimeout(() => node.remove(), 4500);
}
const fail = (error) => toast(error.code ? `${error.code}: ${error.message}` : error.message, "bad");

// ---- Reconciliation badge -------------------------------------------------

async function loadReconciliation() {
  const badge = $("#recon");
  try {
    const report = await api("GET", "/v1/reconciliation");
    const totals = report.trial_balance.map((t) => `${money(t.debits, t.currency)} debits = credits`).join(" · ");
    badge.className = `recon ${report.ok ? "ok" : "bad"}`;
    badge.textContent = report.ok
      ? `✓ Books balanced${totals ? ` · ${totals}` : ""}`
      : `✗ ${Object.values(report.totals).reduce((sum, n) => sum + n, 0).toLocaleString()} discrepancies`;
  } catch {
    badge.className = "recon bad";
    badge.textContent = "Ledger unreachable";
  }
}

// ---- Accounts -------------------------------------------------------------

async function loadAccounts() {
  state.accounts = (await api("GET", "/v1/accounts?limit=200")).data;
  const box = $("#accounts");
  if (!state.accounts.length) {
    box.innerHTML = `<div class="empty">No accounts yet. Run <code>tally demo</code> or create one on the right.</div>`;
  } else {
    box.innerHTML = `<table>
      <thead><tr><th>Account</th><th>Type</th><th class="num">Balance</th><th class="num">Held</th><th class="num">Available</th></tr></thead>
      <tbody>${state.accounts
        .map(
          (a) => `<tr class="clickable" data-id="${a.id}">
            <td><strong>${esc(a.name)}</strong> <span class="muted small">#${a.id}</span></td>
            <td><span class="chip">${a.type}</span> <span class="muted small">${a.normal_side}-normal</span></td>
            <td class="money">${money(a.balance, a.currency)}</td>
            <td class="money muted">${a.held ? money(a.held, a.currency) : "–"}</td>
            <td class="money">${money(a.available, a.currency)}</td></tr>`,
        )
        .join("")}</tbody></table>`;
    box.querySelectorAll("tr[data-id]").forEach((row) => (row.onclick = () => openAccount(Number(row.dataset.id))));
  }
  fillAccountSelects();
}

function fillAccountSelects() {
  const options = state.accounts.map((a) => `<option value="${a.id}">${esc(a.name)} (${a.currency})</option>`).join("");
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

async function loadTransactions() {
  const feed = (await api("GET", "/v1/transactions?limit=15")).data;
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
          ${canReverse ? `<button class="small danger" data-reverse="${t.id}">Reverse</button>` : ""}</div>
        <div class="txn-lines"><span class="muted small">Account</span><span class="muted small num">Debit</span><span class="muted small num">Credit</span>${lines}</div>
      </div>`;
    })
    .join("");
  box.querySelectorAll("[data-reverse]").forEach((button) => {
    button.onclick = async () => {
      try {
        await send("POST", `/v1/transactions/${button.dataset.reverse}/reversal`, { description: "Reversed from the console" });
        toast("Reversed: a mirror-image transaction was posted");
        refresh();
      } catch (error) {
        fail(error);
      }
    };
  });
}

// ---- Forms ----------------------------------------------------------------

document.querySelectorAll(".tab").forEach((tab) => {
  tab.onclick = () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    document.querySelectorAll(".tab-panel").forEach((p) => (p.hidden = p.id !== `form-${tab.dataset.tab}`));
  };
});

$("#form-transfer").onsubmit = async (event) => {
  event.preventDefault();
  const f = event.target;
  try {
    await send("POST", "/v1/transfers", {
      debit_account_id: Number(f.debit.value),
      credit_account_id: Number(f.credit.value),
      amount: toCents(f.amount.value),
      description: f.description.value,
    });
    toast("Transfer posted");
    refresh();
  } catch (error) {
    fail(error);
  }
};

function journalLine(accountId = state.accounts[0]?.id, debit = "", credit = "") {
  const row = document.createElement("tr");
  row.innerHTML = `<td><select class="account-select"></select></td>
    <td><input type="number" min="0" step="0.01" placeholder="0.00" value="${debit}" data-side="debit"></td>
    <td><input type="number" min="0" step="0.01" placeholder="0.00" value="${credit}" data-side="credit"></td>
    <td><button type="button" class="small" aria-label="Remove line">×</button></td>`;
  $("select", row).innerHTML = state.accounts.map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join("");
  if (accountId) $("select", row).value = accountId;
  $("button", row).onclick = () => { row.remove(); updateJournalTotals(); };
  row.querySelectorAll("input").forEach((input) => (input.oninput = updateJournalTotals));
  return row;
}

function resetJournal() {
  const body = $("#journal-lines");
  body.innerHTML = "";
  const [a, b, c] = state.accounts.slice(2, 5).map((acct) => acct.id);
  body.append(journalLine(a, "30.90"), journalLine(b, "", "30.00"), journalLine(c ?? b, "", "0.90"));
  updateJournalTotals();
}

function journalEntries() {
  return [...$("#journal-lines").children].flatMap((row) => {
    const accountId = Number($("select", row).value);
    return [...row.querySelectorAll("input")]
      .filter((input) => Number(input.value) > 0)
      .map((input) => ({ account_id: accountId, direction: input.dataset.side, amount: toCents(input.value) }));
  });
}

function updateJournalTotals() {
  const entries = journalEntries();
  const sum = (side) => entries.filter((e) => e.direction === side).reduce((total, e) => total + e.amount, 0);
  const [debits, credits] = [sum("debit"), sum("credit")];
  $("#total-debit").textContent = money(debits);
  $("#total-credit").textContent = money(credits);
  const check = $("#journal-balance");
  check.className = `balance-check ${debits === credits && debits > 0 ? "ok" : "bad"}`;
  check.textContent = debits === credits && debits > 0
    ? "✓ Balanced: debits equal credits"
    : `✗ Out of balance by ${money(Math.abs(debits - credits))}`;
}

$("#add-line").onclick = () => $("#journal-lines").append(journalLine());

$("#form-journal").onsubmit = async (event) => {
  event.preventDefault();
  try {
    await send("POST", "/v1/transactions", { entries: journalEntries(), description: event.target.description.value });
    toast("Journal entry posted");
    refresh();
  } catch (error) {
    fail(error);
  }
};

$("#form-hold").onsubmit = async (event) => {
  event.preventDefault();
  const f = event.target;
  try {
    await send("POST", "/v1/holds", {
      account_id: Number(f.account.value),
      amount: toCents(f.amount.value),
      expires_in_seconds: Number(f.expires.value),
      description: f.description.value,
    });
    toast("Hold placed: available balance reduced");
    refresh();
  } catch (error) {
    fail(error);
  }
};

$("#form-account").onsubmit = async (event) => {
  event.preventDefault();
  const f = event.target;
  try {
    const account = await api("POST", "/v1/accounts", {
      name: f.name.value,
      type: f.type.value,
      currency: f.currency.value.toUpperCase(),
      allow_negative: f.allow_negative.checked,
    });
    toast(`Created ${account.name} (#${account.id})`);
    f.reset();
    refresh();
  } catch (error) {
    fail(error);
  }
};

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
      <dt>Request</dt><dd><code>${r.method} ${esc(r.path)}</code></dd>
      <dt>Idempotency-Key</dt><dd><code>${esc(r.idempotencyKey)}</code></dd>
    </dl>
    <div class="row end"><button id="replay" class="small">Send again with the same key</button></div>`;
  $("#replay").onclick = async () => {
    try {
      await api(r.method, r.path, r.body, { idempotencyKey: r.idempotencyKey });
      toast(state.lastRequest.replayed === "true" ? "Replayed: same response, no money moved" : "Sent");
      refresh();
    } catch (error) {
      fail(error);
    }
  };
}

// ---- Account detail -------------------------------------------------------

async function openAccount(id) {
  const dialog = $("#account-dialog");
  const box = $("#account-detail");
  const [account, statement, holds] = await Promise.all([
    api("GET", `/v1/accounts/${id}`),
    api("GET", `/v1/accounts/${id}/entries?limit=20`),
    api("GET", `/v1/accounts/${id}/holds`),
  ]);
  const c = account.currency;
  box.innerHTML = `
    <h2>${esc(account.name)} <span class="muted small">#${account.id} · ${account.type}, ${account.normal_side}-normal · ${c}</span></h2>
    <div class="summary">
      <div class="stat"><div class="label">Balance</div><div class="value">${money(account.balance, c)}</div></div>
      <div class="stat"><div class="label">Held</div><div class="value">${money(account.held, c)}</div></div>
      <div class="stat"><div class="label">Available</div><div class="value">${money(account.available, c)}</div></div>
    </div>
    <div class="row">
      <label>Balance as of <input type="datetime-local" id="as-of"></label>
      <label>Result <input id="as-of-result" readonly placeholder="pick a moment"></label>
    </div>
    <h3>Holds</h3>
    ${holds.data.length ? `<table><thead><tr><th>Created</th><th>Description</th><th class="num">Amount</th><th>Status</th><th></th></tr></thead><tbody>
      ${holds.data.map((h) => `<tr><td class="small">${when(h.created_at)}</td><td>${esc(h.description || "–")}</td>
        <td class="money">${money(h.amount, c)}</td><td><span class="chip ${h.status}">${h.status}</span></td>
        <td>${h.status === "active" ? `<button class="small" data-capture="${h.id}" data-amount="${h.amount}">Capture</button> <button class="small danger" data-void="${h.id}">Void</button>` : ""}</td></tr>`).join("")}
      </tbody></table>` : '<div class="empty">No holds on this account.</div>'}
    <h3>Statement</h3>
    ${statement.data.length ? `<table><thead><tr><th>Posted</th><th>Description</th><th class="num">Debit</th><th class="num">Credit</th><th class="num">Balance</th></tr></thead><tbody>
      ${statement.data.map((e) => `<tr><td class="small">${when(e.posted_at)}</td><td>${esc(e.description || "–")}</td>
        <td class="money dr">${e.direction === "debit" ? money(e.amount, c) : ""}</td>
        <td class="money cr">${e.direction === "credit" ? money(e.amount, c) : ""}</td>
        <td class="money">${money(e.balance_after, c)}</td></tr>`).join("")}
      </tbody></table>${statement.next_cursor ? '<p class="muted small">Showing the latest 20 entries (keyset-paginated API).</p>' : ""}` : '<div class="empty">No entries yet.</div>'}`;

  $("#as-of").onchange = async (event) => {
    if (!event.target.value) return;
    const asOf = new Date(event.target.value).toISOString();
    const result = await api("GET", `/v1/accounts/${id}/balance?as_of=${encodeURIComponent(asOf)}`);
    $("#as-of-result").value = money(result.balance, c);
  };
  box.querySelectorAll("[data-capture]").forEach((button) => {
    button.onclick = async () => {
      const destination = prompt(
        `Capture to which account id?\n${state.accounts.filter((a) => a.id !== id).map((a) => `${a.id}: ${a.name}`).join("\n")}`,
      );
      if (!destination) return;
      const amount = prompt(`Amount to capture in dollars (hold is ${money(Number(button.dataset.amount), c)})`, (button.dataset.amount / 100).toFixed(2));
      if (!amount) return;
      try {
        await send("POST", `/v1/holds/${button.dataset.capture}/capture`, { destination_account_id: Number(destination), amount: toCents(amount) });
        toast("Captured: posted the transaction and released the rest of the hold");
        refresh();
        openAccount(id);
      } catch (error) {
        fail(error);
      }
    };
  });
  box.querySelectorAll("[data-void]").forEach((button) => {
    button.onclick = async () => {
      try {
        await api("POST", `/v1/holds/${button.dataset.void}/void`);
        toast("Hold voided: funds available again");
        refresh();
        openAccount(id);
      } catch (error) {
        fail(error);
      }
    };
  });
  if (!dialog.open) dialog.showModal();
}

$("#close-dialog").onclick = () => $("#account-dialog").close();

// ---- Refresh --------------------------------------------------------------

async function refresh() {
  try {
    await loadAccounts();
    await Promise.all([loadTransactions(), loadReconciliation()]);
  } catch (error) {
    fail(error);
  }
}

setInterval(() => !document.hidden && !$("#account-dialog").open && refresh(), 4000);
refresh();
