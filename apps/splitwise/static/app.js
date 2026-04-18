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

let friends = [];
const friendNameById = new Map();

const dollars = (cents) => `$${(cents / 100).toFixed(2)}`;

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
    meta.textContent = `${dollars(e.amount_cents)} · paid by ${e.payer ?? "?"} · for ${who}`;

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

function renderDebts(edges) {
    if (!edges.length) {
        debtsBox.replaceChildren(
            Object.assign(document.createElement("small"), {
                className: "muted",
                textContent: "No debts yet.",
            })
        );
        return;
    }
    const ul = document.createElement("ul");
    ul.className = "debts-list";
    for (const e of edges) {
        const li = document.createElement("li");
        li.innerHTML = `<strong>${e.debtor}</strong> owes <strong>${e.creditor}</strong> <span class="amt">${dollars(e.amount_cents)}</span>`;
        ul.appendChild(li);
    }
    debtsBox.replaceChildren(ul);
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
                render: (r) => dollars(r.total_paid),
            },
        ],
        rows
    );
}

async function refreshDebts() {
    const edges = await api.get(API("/debts"));
    renderDebts(edges);
}

async function refreshAll() {
    await refreshFriends();
    await Promise.all([refreshExpenses(), refreshSummary(), refreshDebts()]);
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
    const { description, amount_cents, payer_id } = e.detail;
    if (!description?.trim() || !payer_id) return;
    const amount = parseInt(amount_cents, 10);
    if (!Number.isFinite(amount) || amount <= 0) return;
    const participant_ids = getSelectedParticipantIds();
    if (!participant_ids.length) {
        alert("Pick at least one person to split among.");
        return;
    }
    await api.post(API("/expenses"), {
        description,
        amount_cents: amount,
        payer_id,
        participant_ids,
    });
    expenseForm.reset();
    renderParticipantPicker();
    notifyHost("expenses.changed", { description, amount_cents: amount });
    refreshAll();
});

refreshAll();
