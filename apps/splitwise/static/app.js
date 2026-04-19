import { api, notifyHost } from "/ui/kit.js";

const APP_BASE = new URL("..", window.location.href).pathname.replace(/\/$/, "");
const API = (p) => `${APP_BASE}/api${p}`;

const friendForm = document.getElementById("friend-form");
const friendsList = document.getElementById("friends");
const expenseForm = document.getElementById("expense-form");
const expensesList = document.getElementById("expenses");
const payerSelect = document.getElementById("payer-select");
const participantsBox = document.getElementById("participants");
const summaryTable = document.getElementById("summary");
const debtsBox = document.getElementById("debts");
const settlementsList = document.getElementById("settlements");

let friends = [];
const friendNameById = new Map();

const euros = (cents) => `€${(cents / 100).toFixed(2)}`;

function friendRow(f) {
    const row = document.createElement("div");
    row.className = "row";
    const name = document.createElement("span");
    name.textContent = f.name;
    const del = document.createElement("button");
    del.className = "icon";
    del.textContent = "×";
    del.title = "Remove friend";
    del.addEventListener("click", async () => {
        await api.del(API(`/friends/${f.id}`));
        notifyHost("friends.changed");
        refreshAll();
    });
    row.append(name, del);
    return row;
}

function expenseRow(e) {
    const row = document.createElement("div");
    row.className = "row";
    const left = document.createElement("div");
    left.className = "expense-main";
    const desc = document.createElement("span");
    desc.textContent = e.description;

    const who = (e.participant_ids ?? [])
        .map((id) => friendNameById.get(id) ?? "?")
        .join(", ") || "—";
    const meta = document.createElement("small");
    meta.className = "muted";
    meta.textContent = `${euros(e.amount_cents)} · paid by ${e.payer ?? "?"} · for ${who}`;

    left.append(desc, meta);
    const del = document.createElement("button");
    del.className = "icon";
    del.textContent = "×";
    del.addEventListener("click", async () => {
        await api.del(API(`/expenses/${e.id}`));
        notifyHost("expenses.changed");
        refreshAll();
    });
    row.append(left, del);
    return row;
}

function renderParticipantPicker() {
    if (!friends.length) {
        participantsBox.replaceChildren(
            Object.assign(document.createElement("small"), {
                className: "muted",
                textContent: "Add a friend first.",
            })
        );
        return;
    }
    // Preserve any user-unchecked selections across re-renders.
    const unchecked = new Set(
        [...participantsBox.querySelectorAll('input[type="checkbox"]')]
            .filter((cb) => !cb.checked)
            .map((cb) => cb.value)
    );
    participantsBox.replaceChildren(
        ...friends.map((f) => {
            const label = document.createElement("label");
            label.className = "participant";
            const cb = document.createElement("input");
            cb.type = "checkbox";
            cb.value = f.id;
            cb.checked = !unchecked.has(f.id);
            const span = document.createElement("span");
            span.textContent = f.name;
            label.append(cb, span);
            return label;
        })
    );
}

function getSelectedParticipantIds() {
    return [...participantsBox.querySelectorAll('input[type="checkbox"]:checked')]
        .map((cb) => cb.value);
}

// Deterministic pastel from a name so Alice is always the same swatch.
function avatarColor(name) {
    let h = 0;
    for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) | 0;
    return `hsl(${Math.abs(h) % 360} 55% 62%)`;
}

function initials(name) {
    const parts = name.trim().split(/\s+/);
    const first = parts[0]?.[0] ?? "?";
    const last = parts.length > 1 ? parts[parts.length - 1][0] : "";
    return (first + last).toUpperCase();
}

function avatar(name) {
    const el = document.createElement("span");
    el.className = "avatar";
    el.style.background = avatarColor(name);
    el.textContent = initials(name);
    el.title = name;
    return el;
}

async function settleUp(edge) {
    const full = (edge.amount_cents / 100).toFixed(2);
    const raw = prompt(
        `${edge.debtor} pays ${edge.creditor} — amount in €:`,
        full
    );
    if (raw == null) return;
    const amount_cents = Math.round(parseFloat(raw) * 100);
    if (!Number.isFinite(amount_cents) || amount_cents <= 0) {
        alert("Enter a positive amount.");
        return;
    }
    await api.post(API("/settlements"), {
        from_friend: edge.debtor_id,
        to_friend: edge.creditor_id,
        amount_cents,
    });
    notifyHost("settlements.changed", { amount_cents });
    refreshAll();
}

function renderDebts(edges) {
    if (!edges.length) {
        debtsBox.replaceChildren(
            Object.assign(document.createElement("div"), {
                className: "debts-empty",
                innerHTML: "✨ <span>All settled up.</span>",
            })
        );
        return;
    }
    const wrap = document.createElement("div");
    wrap.className = "debts-flow";
    for (const e of edges) {
        const row = document.createElement("div");
        row.className = "debt";

        const debtor = document.createElement("div");
        debtor.className = "person";
        debtor.append(avatar(e.debtor), Object.assign(document.createElement("span"), { className: "name", textContent: e.debtor }));

        const arrow = document.createElement("div");
        arrow.className = "arrow";
        arrow.innerHTML = `
            <span class="line"></span>
            <span class="amt-pill">${euros(e.amount_cents)}</span>
            <span class="line"></span>
            <span class="tip">›</span>
        `;

        const creditor = document.createElement("div");
        creditor.className = "person";
        creditor.append(avatar(e.creditor), Object.assign(document.createElement("span"), { className: "name", textContent: e.creditor }));

        const settle = document.createElement("button");
        settle.className = "settle-btn";
        settle.textContent = "Settle up";
        settle.addEventListener("click", () => settleUp(e));

        row.append(debtor, arrow, creditor, settle);
        wrap.appendChild(row);
    }
    debtsBox.replaceChildren(wrap);
}

function settlementRow(s) {
    const row = document.createElement("div");
    row.className = "row";
    const left = document.createElement("div");
    left.className = "expense-main";
    const title = document.createElement("span");
    title.textContent = `${s.from_name ?? "?"} → ${s.to_name ?? "?"}`;
    const meta = document.createElement("small");
    meta.className = "muted";
    meta.textContent = `${euros(s.amount_cents)}${s.note ? " · " + s.note : ""}`;
    left.append(title, meta);
    const del = document.createElement("button");
    del.className = "icon";
    del.textContent = "×";
    del.title = "Remove settlement";
    del.addEventListener("click", async () => {
        await api.del(API(`/settlements/${s.id}`));
        notifyHost("settlements.changed");
        refreshAll();
    });
    row.append(left, del);
    return row;
}

async function refreshFriends() {
    friends = await api.get(API("/friends"));
    friendNameById.clear();
    for (const f of friends) friendNameById.set(f.id, f.name);

    friendsList.render(friends, friendRow);
    payerSelect.replaceChildren(
        ...friends.map((f) => {
            const o = document.createElement("option");
            o.value = f.id;
            o.textContent = f.name;
            return o;
        })
    );
    renderParticipantPicker();
}

async function refreshExpenses() {
    const expenses = await api.get(API("/expenses"));
    expensesList.render(expenses, expenseRow);
}

async function refreshSummary() {
    const rows = await api.get(API("/summary"));
    summaryTable.render(
        [
            { key: "name", label: "Friend" },
            {
                key: "total_paid",
                label: "Total paid",
                render: (r) => euros(r.total_paid),
            },
        ],
        rows
    );
}

async function refreshDebts() {
    const edges = await api.get(API("/debts"));
    renderDebts(edges);
}

async function refreshSettlements() {
    const rows = await api.get(API("/settlements"));
    settlementsList.render(rows, settlementRow);
}

async function refreshAll() {
    await refreshFriends();
    await Promise.all([
        refreshExpenses(),
        refreshSummary(),
        refreshDebts(),
        refreshSettlements(),
    ]);
}

friendForm.addEventListener("hs-submit", async (e) => {
    const { name } = e.detail;
    if (!name?.trim()) return;
    await api.post(API("/friends"), { name });
    friendForm.reset();
    notifyHost("friends.changed", { name });
    refreshAll();
});

expenseForm.addEventListener("hs-submit", async (e) => {
    const { description, amount, payer_id } = e.detail;
    if (!description?.trim() || !payer_id) return;
    // Round to the nearest cent — parseFloat("12.1") * 100 = 1209.9999... otherwise.
    const amount_cents = Math.round(parseFloat(amount) * 100);
    if (!Number.isFinite(amount_cents) || amount_cents <= 0) return;
    const participant_ids = getSelectedParticipantIds();
    if (!participant_ids.length) {
        alert("Pick at least one person to split among.");
        return;
    }
    await api.post(API("/expenses"), {
        description,
        amount_cents,
        payer_id,
        participant_ids,
    });
    expenseForm.reset();
    renderParticipantPicker();
    notifyHost("expenses.changed", { description, amount_cents });
    refreshAll();
});

refreshAll();
