let routines = [];
let editingId = null;

// ── Bootstrap ────────────────────────────────────────────────
loadRoutines();
document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("f-schedule").addEventListener("input", updateScheduleHuman);
});

// ── Data ─────────────────────────────────────────────────────
async function loadRoutines() {
    try {
        const res = await fetch("/api/routines");
        routines = await res.json();
        renderRoutines();
    } catch (e) {
        console.error("Failed to load routines:", e);
    }
}

// ── Render ───────────────────────────────────────────────────
function renderRoutines() {
    const el = document.getElementById("routines-list");
    if (!routines.length) {
        el.innerHTML = '<div class="empty-state">No routines yet. Create one to get started.</div>';
        return;
    }
    el.innerHTML = routines.map(r => routineCard(r)).join("");
}

function routineCard(r) {
    const nextRun = r.next_run_at ? fmtRelative(r.next_run_at) : (r.enabled ? "—" : "paused");
    const lastRun = r.last_run_at ? fmtRelative(r.last_run_at) : "never";
    return `
    <div class="routine-card ${r.enabled ? "" : "disabled"}" id="card-${r.id}">
        <div class="card-main">
            <div class="card-left">
                <div class="card-name">${escHtml(r.name)}</div>
                <div class="card-schedule">${escHtml(r.schedule)} · ${humanCron(r.schedule)}</div>
                <div class="card-prompt">${escHtml(r.prompt.slice(0, 140))}${r.prompt.length > 140 ? "…" : ""}</div>
            </div>
            <div class="card-right">
                <div class="card-meta">
                    <span class="meta-label">next</span>
                    <span class="meta-val">${nextRun}</span>
                </div>
                <div class="card-meta">
                    <span class="meta-label">last</span>
                    <span class="meta-val">${lastRun}</span>
                </div>
            </div>
        </div>
        <div class="card-actions">
            <label class="toggle" title="${r.enabled ? "Disable" : "Enable"}">
                <input type="checkbox" ${r.enabled ? "checked" : ""} onchange="toggleEnabled('${r.id}', this.checked)" />
                <span class="toggle-track"></span>
            </label>
            <div class="card-actions-spacer"></div>
            <button class="action-btn" onclick="openRuns('${r.id}', '${escHtml(r.name)}')">history</button>
            <button class="action-btn" onclick="triggerRun('${r.id}', event)">run now</button>
            <button class="action-btn" onclick="openEdit('${r.id}')">edit</button>
            <button class="action-btn danger" onclick="deleteRoutine('${r.id}')">delete</button>
        </div>
    </div>`;
}

// ── Modal ────────────────────────────────────────────────────
function openModal(id = null) {
    editingId = id;
    const r = id ? routines.find(r => r.id === id) : null;
    document.getElementById("modal-title").textContent = id ? "Edit Routine" : "New Routine";
    document.getElementById("f-name").value = r ? r.name : "";
    document.getElementById("f-schedule").value = r ? r.schedule : "0 8 * * *";
    document.getElementById("f-prompt").value = r ? r.prompt : "";
    document.getElementById("f-enabled").checked = r ? r.enabled : true;
    document.getElementById("save-btn").textContent = id ? "Update" : "Save";
    updateScheduleHuman();
    document.getElementById("modal-backdrop").classList.add("visible");
    document.getElementById("modal").classList.add("visible");
    setTimeout(() => document.getElementById("f-name").focus(), 50);
}

function openEdit(id) { openModal(id); }

function closeModal() {
    document.getElementById("modal-backdrop").classList.remove("visible");
    document.getElementById("modal").classList.remove("visible");
    editingId = null;
}

async function saveRoutine(e) {
    e.preventDefault();
    const btn = document.getElementById("save-btn");
    btn.disabled = true;
    const body = {
        name:     document.getElementById("f-name").value.trim(),
        schedule: document.getElementById("f-schedule").value.trim(),
        prompt:   document.getElementById("f-prompt").value.trim(),
        enabled:  document.getElementById("f-enabled").checked,
    };
    try {
        const res = await fetch(editingId ? `/api/routines/${editingId}` : "/api/routines", {
            method: editingId ? "PATCH" : "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || res.statusText);
        }
        closeModal();
        await loadRoutines();
    } catch (err) {
        alert("Failed to save: " + err.message);
    } finally {
        btn.disabled = false;
    }
}

// ── Actions ──────────────────────────────────────────────────
async function toggleEnabled(id, enabled) {
    await fetch(`/api/routines/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
    });
    await loadRoutines();
}

async function deleteRoutine(id) {
    const r = routines.find(r => r.id === id);
    if (!confirm(`Delete "${r?.name}"? This will also delete all run history.`)) return;
    await fetch(`/api/routines/${id}`, { method: "DELETE" });
    await loadRoutines();
}

async function triggerRun(id, e) {
    const btn = e.target;
    btn.disabled = true;
    btn.textContent = "running…";
    try {
        await fetch(`/api/routines/${id}/run`, { method: "POST" });
        setTimeout(() => loadRoutines(), 800);
    } catch (err) {
        alert("Failed to trigger: " + err.message);
    } finally {
        setTimeout(() => {
            btn.disabled = false;
            btn.textContent = "run now";
        }, 2000);
    }
}

// ── Runs panel ───────────────────────────────────────────────
async function openRuns(id, name) {
    document.getElementById("panel-title").textContent = `${name} — history`;
    document.getElementById("panel-body").innerHTML = '<div class="empty-state">Loading…</div>';
    document.getElementById("panel-backdrop").classList.add("visible");
    document.getElementById("panel").classList.add("visible");

    try {
        const res = await fetch(`/api/routines/${id}/runs`);
        const runs = await res.json();
        const body = document.getElementById("panel-body");
        if (!runs.length) {
            body.innerHTML = '<div class="empty-state">No runs yet. Click "run now" to trigger one.</div>';
            return;
        }
        body.innerHTML = runs.map(run => `
            <div class="run-row status-${run.status}">
                <div class="run-header">
                    <span class="run-status">${run.status}</span>
                    <span class="run-time">${fmtDate(run.started_at)}</span>
                    ${run.conversation_id
                        ? `<a class="run-link" href="/?conv=${run.conversation_id}" target="_blank">open chat ↗</a>`
                        : ""}
                </div>
                ${run.output
                    ? `<div class="run-output">${escHtml(run.output.slice(0, 400))}${run.output.length > 400 ? "…" : ""}</div>`
                    : ""}
                ${run.error
                    ? `<div class="run-error">${escHtml(run.error)}</div>`
                    : ""}
            </div>
        `).join("");
    } catch (err) {
        document.getElementById("panel-body").innerHTML = `<div class="run-error">Failed to load: ${escHtml(err.message)}</div>`;
    }
}

function closePanel() {
    document.getElementById("panel-backdrop").classList.remove("visible");
    document.getElementById("panel").classList.remove("visible");
}

// ── Schedule helpers ─────────────────────────────────────────
function applyPreset(sel) {
    if (sel.value) {
        document.getElementById("f-schedule").value = sel.value;
        updateScheduleHuman();
    }
    sel.value = "";
}

function updateScheduleHuman() {
    const el = document.getElementById("schedule-human");
    if (!el) return;
    const val = document.getElementById("f-schedule").value.trim();
    el.textContent = val ? humanCron(val) : "";
}

function humanCron(expr) {
    const known = {
        "0 8 * * *":    "Every day at 8:00 AM UTC",
        "0 9 * * *":    "Every day at 9:00 AM UTC",
        "0 12 * * *":   "Every day at noon UTC",
        "0 0 * * *":    "Every day at midnight UTC",
        "0 8 * * 1":    "Every Monday at 8:00 AM UTC",
        "0 9 * * 1-5":  "Weekdays at 9:00 AM UTC",
        "0 */4 * * *":  "Every 4 hours",
        "0 * * * *":    "Every hour",
        "*/15 * * * *": "Every 15 minutes",
        "*/5 * * * *":  "Every 5 minutes",
        "0 0 * * 0":    "Every Sunday at midnight UTC",
        "0 0 1 * *":    "First of every month",
    };
    return known[expr] || "";
}

// ── Date utils ───────────────────────────────────────────────
function fmtRelative(iso) {
    const diff = Date.now() - new Date(iso).getTime();
    const abs = Math.abs(diff);
    const future = diff < 0;
    if (abs < 60000)    return future ? "in a moment" : "just now";
    if (abs < 3600000)  return (future ? "in " : "") + Math.round(abs / 60000) + "m" + (future ? "" : " ago");
    if (abs < 86400000) return (future ? "in " : "") + Math.round(abs / 3600000) + "h" + (future ? "" : " ago");
    return (future ? "in " : "") + Math.round(abs / 86400000) + "d" + (future ? "" : " ago");
}

function fmtDate(iso) {
    return new Date(iso).toLocaleString([], { dateStyle: "short", timeStyle: "short" });
}

// ── Keyboard shortcuts ───────────────────────────────────────
document.addEventListener("keydown", e => {
    if (e.key === "Escape") { closeModal(); closePanel(); }
});
