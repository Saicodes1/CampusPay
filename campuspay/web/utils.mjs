export function cents(value) {
  const raw = String(value).trim();
  if (!/^(?:0|[1-9]\d*)(?:\.\d{1,2})?$/.test(raw)) throw new Error("Enter an amount with up to two decimal places.");
  const [whole, fraction = ""] = raw.split(".");
  const amount = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
  if (!Number.isSafeInteger(amount) || amount < 1 || amount > 1_000_000) throw new Error("Enter an amount between $0.01 and $10,000.00.");
  return amount;
}

export function money(amount) {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(amount / 100);
}

export function intentId(value) {
  const raw = value.trim().replace(/^campuspay:intent:/i, "");
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(raw)) throw new Error("Paste a valid payment request ID or campuspay:intent: code.");
  return raw.toLowerCase();
}

export function restorePending(raw, actor) {
  try {
    const p = JSON.parse(raw);
    const path = /^\/api\/(?:funding|transfers|student\/purchases|intents\/[0-9a-f-]{36}\/(?:authorize|void))$/;
    if (p.actor !== actor || !path.test(p.path) || !/^[0-9a-f-]{36}$/.test(p.key) || typeof p.label !== "string") return null;
    if (p.body !== null && (typeof p.body !== "object" || Array.isArray(p.body))) return null;
    return {actor: p.actor, path: p.path, key: p.key, body: p.body, label: p.label};
  } catch { return null; }
}

export function description(entry) {
  const incoming = entry.side === "CREDIT";
  if (entry.kind === "FUNDING") return "Added demo funds";
  if (entry.kind === "CAPTURE") return `Paid ${entry.counterparty}`;
  if (entry.kind === "REFUND") return `Refund from ${entry.counterparty}`;
  if (entry.kind === "TRANSFER") return `${incoming ? "From" : "To"} ${entry.counterparty}`;
  return "Wallet transaction";
}
