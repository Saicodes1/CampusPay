import {cents, money, intentId, restorePending, description} from "/assets/utils.mjs";

const $ = (selector) => document.querySelector(selector);
const state = {user:null, credential:null, wallet:null, activity:[], holds:[], catalog:[], contacts:[], cursor:null, busy:false, loading:false, pending:null, action:null, epoch:0, dialogVersion:0};
const pages = ["overview", "activity", "payments", "account"];
const messages = {
  INVALID_CREDENTIALS:"That password didn’t match. Please try again.",
  AUTHENTICATION_REQUIRED:"Sign in to continue.",
  FORBIDDEN:"This action isn’t available for your student account.",
  INSUFFICIENT_AVAILABLE_FUNDS:"You don’t have enough available funds. Add demo funds or release an existing authorization.",
  IDEMPOTENCY_PAYLOAD_MISMATCH:"This request no longer matches its original payment. Refresh your activity before starting another.",
  INVALID_STATE:"This payment has already changed. Refresh your wallet to see its current status.",
  AUTHORIZATION_EXPIRED:"This authorization has expired. Ask the merchant for a new payment request.",
  NOT_FOUND:"That payment request wasn’t found. Check the code and try again.",
  RATE_LIMITED:"Too many requests. Wait a minute before trying again.",
  RATE_LIMIT_SERVICE_UNAVAILABLE:"Sign-in checks are temporarily unavailable. Please try again shortly.",
  DATABASE_UNAVAILABLE_OR_CONFLICT:"The wallet service is temporarily unavailable. Retry the same request shortly.",
  PRICE_CHANGED:"The price has changed. Refresh the shop before paying.",
  PRODUCT_NOT_FOUND:"This item is no longer available. Refresh the shop.",
  VALIDATION_ERROR:"Check the amount and required fields, then try again."
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `/assets/icons.svg#${name}`);
  svg.append(use);
  return svg;
}
function button(text, className = "button", click) {
  const b = el("button", className, text);
  b.type = "button";
  if (click) b.addEventListener("click", click);
  return b;
}
function mutateButton(text, click, className = "button") {
  const b = button(text, className, click);
  b.dataset.mutates = "";
  b.disabled = state.busy || state.loading || !!state.pending;
  return b;
}
function showError(target, message) { target.textContent = message; target.hidden = !message; }
function notice(text) { $("#notification-text").textContent = text; $("#notification").hidden = false; }

async function api(path, {method = "GET", body = null, key = null} = {}) {
  const headers = {Authorization:`Basic ${state.credential}`};
  if (body !== null) headers["Content-Type"] = "application/json";
  if (key) headers["Idempotency-Key"] = key;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch(path, {method, headers, body:body === null ? undefined : JSON.stringify(body), credentials:"omit", cache:"no-store", signal:controller.signal});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(messages[data.code] || "We couldn’t complete the request. Please try again.");
      error.status = response.status;
      throw error;
    }
    return data;
  } catch (error) {
    if (!error.status) throw new Error("We couldn’t reach CampusPay. Check your connection and try again.");
    throw error;
  } finally { clearTimeout(timeout); }
}

function pendingKey() { return `campuspay.pending.${state.user}`; }
function savePending(plan) {
  state.pending = plan;
  // Only request metadata is persisted; credentials never leave this tab's memory.
  try { if (plan) sessionStorage.setItem(pendingKey(), JSON.stringify(plan)); else sessionStorage.removeItem(pendingKey()); } catch { /* Still safe to retry while this tab stays open. */ }
  updateControls();
}
function updateControls() {
  document.querySelectorAll("[data-mutates]").forEach(b => b.disabled = state.busy || state.loading || !!state.pending);
  $("#pending-banner").hidden = !state.pending;
  if (state.pending) {
    const amount = state.pending.body?.amount ?? state.pending.body?.expected_amount;
    $("#pending-summary").textContent = `${state.pending.label}${amount ? " " + money(amount) : ""} This request may have gone through. Retry it safely to check.`;
  }
  $("#retry-pending").disabled = state.busy;
  $("#retry-pending").textContent = state.busy ? "Checking…" : "Check payment";
  $("#refresh").disabled = state.loading || state.busy;
  $("#sign-out").disabled = state.busy;
}

async function refresh() {
  if (state.loading || !state.user) return;
  const epoch = state.epoch;
  state.loading = true; updateControls();
  try {
    const [wallet, activity, holds, catalog, contacts] = await Promise.all([
      api("/api/wallet"), api("/api/student/activity"), api("/api/student/authorizations"), api("/api/student/catalog"), api("/api/student/contacts")
    ]);
    if (epoch !== state.epoch) return;
    Object.assign(state, {wallet, activity:activity.items, cursor:activity.nextCursor, holds:holds.items, catalog:catalog.items, contacts:contacts.items});
    render();
    showError($("#load-error"), "");
    $("#last-updated").textContent = `Updated ${new Intl.DateTimeFormat(undefined,{hour:"2-digit", minute:"2-digit"}).format(new Date())}`;
  } catch (error) {
    if (epoch === state.epoch) showError($("#load-error"), `Couldn’t refresh your wallet. ${error.message} Displayed amounts may be out of date.`);
  } finally { if (epoch === state.epoch) { state.loading = false; updateControls(); } }
}

function table(entries, target, emptyTitle = "No activity yet", emptyText = "Add demo funds to start using your wallet.") {
  target.replaceChildren();
  if (!entries.length) {
    const empty = el("div", "empty-state");
    empty.append(icon("activity"), el("strong", "", emptyTitle), el("p", "", emptyText));
    target.append(empty); return;
  }
  const t = el("table", "activity-table");
  const head = el("thead"); const hr = el("tr");
  ["Transaction", "Date", "Amount"].forEach(text => { const th=el("th", "", text); th.scope="col"; hr.append(th); });
  head.append(hr); t.append(head);
  const body = el("tbody");
  for (const entry of entries) {
    const row = el("tr"); const info = el("td"); const desc = el("div", "transaction-description");
    const incoming = entry.side === "CREDIT";
    const names = {FUNDING:"Simulated funding",CAPTURE:"Campus payment",REFUND:"Refund",TRANSFER:"Student transfer"};
    const detail = el("div"); detail.append(el("strong", "", description(entry)), el("span", "", `${names[entry.kind] || "Wallet transaction"} · ${new Intl.DateTimeFormat(undefined,{month:"short", day:"numeric"}).format(new Date(entry.created_at))}`));
    desc.append(icon(incoming ? "arrow-in" : "arrow-out"), detail); info.append(desc);
    const date = el("td", "date-cell", new Intl.DateTimeFormat(undefined,{dateStyle:"medium", timeStyle:"short"}).format(new Date(entry.created_at)));
    const amount = el("td", `amount-cell${incoming ? " incoming" : ""}`, `${incoming ? "+" : "−"}${money(entry.amount)}`);
    row.append(info, date, amount); body.append(row);
  }
  t.append(body); target.append(t);
}

function renderActivity() {
  table(state.activity.slice(0,6), $("#recent-activity"));
  const query = $("#activity-search").value.trim().toLowerCase();
  const filter = $("#activity-filter").value;
  const entries = state.activity.filter(e => (filter === "all" || e.kind === filter) && `${description(e)} ${e.kind}`.toLowerCase().includes(query));
  table(entries, $("#all-activity"), state.activity.length ? "No matching transactions" : "No activity yet", state.activity.length ? "Try another search or load older activity." : "Add demo funds to start using your wallet.");
  $("#load-more").hidden = state.cursor === null;
}

function render() {
  if (!state.wallet) return;
  const w = state.wallet;
  $("#available-balance").textContent = money(w.available);
  $("#total-balance").textContent = money(w.balance);
  $("#reserved-balance").textContent = money(w.reserved);
  $("#student-name").textContent = w.owner_name;
  $("#student-initial").textContent = w.owner_name[0];
  $("#profile-name").textContent = w.owner_name;
  $("#profile-id").textContent = w.id;
  renderActivity(); renderCatalog(); renderHolds(); updateControls();
}

function renderCatalog() {
  const catalog = $("#catalog"); catalog.replaceChildren();
  for (const product of state.catalog) {
    const card = el("article", "panel product"); const merchant = el("div", "product-merchant");
    merchant.append(icon(product.merchant_id === "cafe" ? "coffee" : "book"), el("span", "", product.merchant_name));
    const footer = el("div", "product-footer");
    footer.append(el("strong", "product-price", money(product.amount)), mutateButton("Review purchase", () => openPurchase(product), "button"));
    card.append(merchant, el("h3", "", product.name), el("p", "", product.description), footer); catalog.append(card);
  }
  const shortcuts = $("#merchant-shortcuts"); shortcuts.replaceChildren();
  const seen = new Set();
  for (const product of state.catalog) {
    if (seen.has(product.merchant_id)) continue; seen.add(product.merchant_id);
    const b = button("", "merchant-shortcut", () => { location.hash = "payments"; });
    const description = el("span"); description.append(el("strong", "", product.merchant_name), el("small", "", product.category));
    b.append(icon(product.merchant_id === "cafe" ? "coffee" : "book"), description, el("span", "arrow", "→")); shortcuts.append(b);
  }
}

function renderHolds() {
  $("#overview-holds").hidden = !state.holds.length;
  const list = $("#holds-list"); list.replaceChildren();
  for (const hold of state.holds) {
    const row=el("div", "hold-row"); const details=el("div", "hold-description");
    const expired = new Date(hold.expires_at) <= new Date();
    details.append(el("strong", "", hold.merchant_name), el("small", "", expired ? "Expired · awaiting release" : `Reserved until ${new Intl.DateTimeFormat(undefined,{hour:"2-digit",minute:"2-digit"}).format(new Date(hold.expires_at))}`));
    row.append(icon("clock"), details, el("span", "amount", money(hold.amount)), mutateButton(expired ? "Release hold" : "Cancel authorization", () => openVoid(hold), "button small"));
    list.append(row);
  }
}

function navigate() {
  const page = pages.includes(location.hash.slice(1)) ? location.hash.slice(1) : "overview";
  document.querySelectorAll(".page").forEach(p => p.hidden = p.id !== `page-${page}`);
  document.querySelectorAll(".nav-link").forEach(a => { const active=a.dataset.page === page; a.classList.toggle("active", active); if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
  document.title = `CampusPay · ${{overview:"My wallet", activity:"Activity", payments:"Campus payments", account:"Your account"}[page]}`;
  if (state.user) window.scrollTo({top:0, behavior:"instant"});
}

function openDialog(title, submit, action) {
  if (state.loading) { notice("Your wallet is refreshing. Please try again in a moment."); return false; }
  if (state.busy || state.pending) { notice("Check your pending payment before starting another."); return false; }
  state.action = action;
  state.dialogVersion++;
  $("#dialog-title").textContent = title;
  $("#dialog-body").replaceChildren();
  $("#dialog-submit").textContent = submit;
  $("#dialog-submit").disabled = false;
  $("#dialog-submit").hidden = false;
  showError($("#dialog-error"), "");
  if (!$("#action-dialog").open) $("#action-dialog").showModal();
  return true;
}
function field(label, id, tag = "input") {
  const l=el("label", "", label); l.htmlFor=id;
  const input=el(tag); input.id=id; input.required=true;
  $("#dialog-body").append(l, input); return input;
}
function amountField() {
  const input=field("Amount (USD)", "action-amount"); input.inputMode="decimal"; input.placeholder="0.00"; input.autocomplete="off";
  const wrapper=el("div", "amount-input"); input.replaceWith(wrapper); wrapper.append(el("span", "", "$"), input);
  return input;
}
function review(rows) {
  const box=el("div", "review-summary");
  for (const [label,value] of rows) { const row=el("div"); row.append(el("span", "", label), el("strong", "", value)); box.append(row); }
  $("#dialog-body").append(box);
}
function openFund() {
  if (!openDialog("Add demo funds", "Add demo funds", "fund")) return;
  $("#dialog-body").append(el("p", "", "Add simulated money to your student wallet. No card or bank account is charged."));
  const input=amountField();
  const presets=el("div", "preset-row"); [10,25,50].forEach(n => presets.append(button(`$${n}`, "button", () => { input.value=n.toFixed(2); input.focus(); })));
  $("#dialog-body").append(presets); input.focus();
}
function openTransfer() {
  if (!openDialog("Send money", "Review transfer", "transfer")) return;
  $("#dialog-body").append(el("p", "", `Available to send: ${money(state.wallet.available)}. Transfers are between demo student wallets.`));
  const select=field("Recipient", "transfer-recipient", "select");
  for (const contact of state.contacts) { const option=el("option", "", contact.owner_name); option.value=contact.id; select.append(option); }
  if (!state.contacts.length) { showError($("#dialog-error"), "No other student accounts are available. Refresh your wallet."); $("#dialog-submit").disabled=true; }
  amountField();
}
function openPurchase(product) {
  if (!openDialog("Review purchase", `Pay ${money(product.amount)}`, {type:"purchase", product})) return;
  review([["Merchant", product.merchant_name],["Item", product.name],["You pay", money(product.amount)]]);
  $("#dialog-body").append(el("p", "field-note", "This demo merchant captures the payment immediately. Your available wallet balance will decrease by this amount."));
  if (product.amount > state.wallet.available) { showError($("#dialog-error"), "You don’t have enough available funds for this purchase. Add demo funds first."); $("#dialog-submit").disabled=true; }
}
function openIntent() {
  if (!openDialog("Enter payment code", "Review payment", "intent")) return;
  $("#dialog-body").append(el("p", "", "Paste the payment code from a merchant QR, or its payment request ID."));
  const input=field("Payment code or request ID", "intent-code"); input.placeholder="campuspay:intent:…"; input.autocomplete="off"; input.maxLength=80; input.focus();
}
function openVoid(hold) {
  if (!openDialog("Cancel authorization", "Release funds", {type:"void", hold})) return;
  review([["Merchant", hold.merchant_name],["Funds to release", money(hold.amount)]]);
  $("#dialog-body").append(el("p", "field-note", "This cancels the authorization if the merchant hasn’t captured it. The reserved amount returns to your available balance."));
}

async function mutate(path, body, label) {
  if (state.pending || state.busy) return;
  const plan={actor:state.user, path, body, key:crypto.randomUUID(), label};
  $("#notification").hidden = true;
  savePending(plan);
  await executePlan(plan);
}
async function executePlan(plan) {
  if (state.busy) return;
  state.busy=true; updateControls();
  $("#dialog-submit").disabled=true;
  try {
    const response=await api(plan.path, {method:"POST", body:plan.body, key:plan.key});
    savePending(null);
    $("#action-dialog").close();
    notice(`${plan.label} ${money(response.amount)} · ${response.status === "AUTHORIZED" ? "Reserved until the merchant captures or releases it." : "Confirmed."}`);
    await refresh();
  } catch (error) {
    if (error.status && error.status < 500 && ![401,429].includes(error.status)) {
      savePending(null);
      if ($("#action-dialog").open) showError($("#dialog-error"), error.message);
      else showError($("#load-error"), error.message);
    } else {
      $("#action-dialog").close();
      showError($("#load-error"), `${error.message} Use “Check payment” to retry with the original request.`);
    }
  } finally { state.busy=false; $("#dialog-submit").disabled=false; updateControls(); }
}

$("#action-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (state.busy) return;
  showError($("#dialog-error"), "");
  try {
    const action=state.action;
    if (action === "fund") { const amount=cents($("#action-amount").value); await mutate("/api/funding", {amount}, "Added demo funds:"); }
    else if (action === "transfer") {
      const amount=cents($("#action-amount").value), recipient=$("#transfer-recipient").value;
      if (amount > state.wallet.available) throw new Error("You don’t have enough available funds for this transfer.");
      const contact=state.contacts.find(c => c.id === recipient); if (!contact) throw new Error("Choose a recipient.");
      openDialog("Review transfer", `Send ${money(amount)}`, {type:"transfer-confirm", amount, recipient});
      review([["To", contact.owner_name],["You send", money(amount)]]);
      $("#dialog-body").append(el("p", "field-note", "The transfer is immediate and uses simulated funds."));
    } else if (action === "intent") {
      const id=intentId($("#intent-code").value);
      const version=state.dialogVersion, epoch=state.epoch;
      $("#dialog-submit").disabled=true;
      try {
        const intent=await api(`/api/intents/${id}`);
        if (version !== state.dialogVersion || epoch !== state.epoch || !$("#action-dialog").open) return;
        if (intent.status !== "CREATED") throw new Error("This payment request is no longer awaiting authorization.");
        const merchant=state.catalog.find(p => p.merchant_id === intent.merchant_id)?.merchant_name || intent.merchant_id;
        openDialog("Authorize payment", `Authorize ${money(intent.amount)}`, {type:"authorize", intent});
        review([["Merchant", merchant],["Amount reserved", money(intent.amount)]]);
        $("#dialog-body").append(el("p", "field-note", "This reserves funds for up to 15 minutes. The merchant captures the payment separately. You can cancel an uncaptured authorization."));
        if (intent.amount > state.wallet.available) { showError($("#dialog-error"), "You don’t have enough available funds. Add demo funds before authorizing."); $("#dialog-submit").disabled=true; }
      } catch (error) { if (version !== state.dialogVersion || epoch !== state.epoch || !$("#action-dialog").open) return; $("#dialog-submit").disabled=false; throw error; }
    } else if (action.type === "purchase") {
      await mutate("/api/student/purchases", {product_id:action.product.id, expected_amount:action.product.amount}, `Paid ${action.product.merchant_name}:`);
    } else if (action.type === "transfer-confirm") {
      await mutate("/api/transfers", {recipient:action.recipient, amount:action.amount}, "Transfer sent:");
    } else if (action.type === "authorize") {
      await mutate(`/api/intents/${action.intent.id}/authorize`, null, "Payment authorized:");
    } else if (action.type === "void") {
      await mutate(`/api/intents/${action.hold.id}/void`, null, "Authorization released:");
    }
  } catch (error) { showError($("#dialog-error"), error.message); }
});

$("#login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const submit=$("#login-submit"); submit.disabled=true; submit.textContent="Signing in…";
  showError($("#login-error"), "");
  state.user=$("#login-account").value;
  state.credential=btoa(String.fromCharCode(...new TextEncoder().encode(`${state.user}:${$("#login-password").value}`)));
  try {
    const wallet=await api("/api/wallet");
    if (wallet.kind !== "STUDENT") throw new Error("Use a student account to access this dashboard.");
    state.wallet=wallet; state.epoch++;
    try { state.pending=restorePending(sessionStorage.getItem(pendingKey()), state.user); } catch { state.pending=null; }
    $("#login-password").value="";
    $("#login-view").hidden=true; $("#app-view").hidden=false;
    navigate(); render(); await refresh(); $("#main").focus({preventScroll:true});
  } catch (error) { state.credential=null; state.user=null; showError($("#login-error"), error.message); }
  finally { submit.disabled=false; submit.textContent="Sign in"; }
});

$("#sign-out").addEventListener("click", () => {
  if (state.busy) return;
  state.epoch++; Object.assign(state, {user:null, credential:null, wallet:null, activity:[], holds:[], catalog:[], contacts:[], cursor:null, pending:null, action:null, loading:false});
  $("#action-dialog").close(); $("#app-view").hidden=true; $("#login-view").hidden=false;
  $("#notification").hidden=true; $("#pending-banner").hidden=true; showError($("#load-error"), "");
  $("#login-password").value=""; $("#login-password").focus(); document.title="CampusPay · Student wallet";
});
$("#refresh").addEventListener("click", refresh);
$("#retry-pending").addEventListener("click", () => { if (state.pending) executePlan(state.pending); });
$("#dismiss-notice").addEventListener("click", () => $("#notification").hidden=true);
$("#activity-search").addEventListener("input", renderActivity);
$("#activity-filter").addEventListener("change", renderActivity);
$("#load-more").addEventListener("click", async () => {
  if (!state.cursor || state.loading) return;
  const b=$("#load-more"), epoch=state.epoch, cursor=state.cursor; b.disabled=true; b.textContent="Loading…";
  try {
    const page=await api(`/api/student/activity?before=${state.cursor}`);
    if (epoch !== state.epoch || cursor !== state.cursor) return;
    const seen=new Set(state.activity.map(e => e.id)); state.activity.push(...page.items.filter(e => !seen.has(e.id))); state.cursor=page.nextCursor; renderActivity();
  } catch (error) { if (epoch === state.epoch) showError($("#load-error"), error.message); }
  finally { b.disabled=false; b.textContent="Load older activity"; }
});
document.querySelectorAll("[data-action]").forEach(b => b.addEventListener("click", () => ({fund:openFund,transfer:openTransfer,intent:openIntent})[b.dataset.action]()));
document.querySelectorAll("[data-close]").forEach(b => b.addEventListener("click", () => { if (!state.busy) $("#action-dialog").close(); }));
$("#action-dialog").addEventListener("cancel", e => { if (state.busy) e.preventDefault(); });
window.addEventListener("hashchange", navigate);
navigate();
