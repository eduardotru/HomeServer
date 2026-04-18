const input = document.getElementById("search-input");
const searchBtn = document.getElementById("search-btn");
const brandDot = document.getElementById("brand-dot");
const statusEl = document.getElementById("status-text");
const grid = document.getElementById("results-grid");
const sourcesListEl = document.getElementById("sources-list");
const threadEl = document.getElementById("search-thread");
const followupRow = document.getElementById("followup-row");
const followupInput = document.getElementById("followup-input");
const followupBtn = document.getElementById("followup-btn");

let currentSessionId = null;
let sourceCount = 0;  // global counter across all search rounds

// ── Init ────────────────────────────────────────────────────
loadHistory();

input.addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });
followupInput.addEventListener("keydown", (e) => { if (e.key === "Enter") doFollowUp(); });

document.addEventListener("click", (e) => {
    const btn = document.getElementById("hist-btn");
    const dd = document.getElementById("hist-dropdown");
    if (!btn.contains(e.target) && !dd.contains(e.target)) closeHistory();
});

// ── State ────────────────────────────────────────────────────
function setSearching(active) {
    searchBtn.disabled = active;
    followupBtn.disabled = active;
    brandDot.className = "brand-dot" + (active ? " searching" : "");
    statusEl.textContent = active ? "searching…" : "";
}

// ── Thread rendering ─────────────────────────────────────────
function addTurnToThread(query) {
    // Query row
    const qEl = document.createElement("div");
    qEl.className = "turn-query";
    qEl.textContent = query;
    threadEl.appendChild(qEl);

    // Searching indicator (shown while fetching / planning)
    const searching = document.createElement("div");
    searching.className = "turn-searching";
    searching.innerHTML = `<span class="searching-dot"></span><span class="searching-label">analyzing…</span>`;
    threadEl.appendChild(searching);

    // Answer block (filled in during streaming)
    const answer = document.createElement("div");
    answer.className = "turn-answer";
    const cursor = document.createElement("span");
    cursor.className = "cursor";
    answer.appendChild(cursor);
    threadEl.appendChild(answer);

    return { searching, answer, cursor };
}

function updateSearchingLabel(el, text) {
    const lbl = el.querySelector(".searching-label");
    if (lbl) lbl.textContent = text;
}

function finalizeSearching(el) {
    el.remove();
}

function appendSources(results) {
    if (!Array.isArray(results) || !results.length) return;
    const wasEmpty = sourcesListEl.querySelector(".sources-empty");
    if (wasEmpty) sourcesListEl.innerHTML = "";
    results.forEach((r) => {
        sourceCount++;
        const a = document.createElement("a");
        a.className = "source-item";
        a.href = escHtml(r.url);
        a.target = "_blank";
        a.rel = "noopener";
        a.innerHTML = `
            <span class="source-num">[${sourceCount}] ${escHtml(hostname(r.url))}</span>
            <span class="source-title">${escHtml(r.title || r.url)}</span>
        `;
        sourcesListEl.appendChild(a);
    });
}

// ── Main search entry ────────────────────────────────────────
async function doSearch() {
    const query = input.value.trim();
    if (!query || searchBtn.disabled) return;

    // First search — reset UI
    currentSessionId = null;
    sourceCount = 0;
    threadEl.innerHTML = "";
    sourcesListEl.innerHTML = '<div class="sources-empty">Fetching results…</div>';
    followupRow.style.display = "none";
    grid.style.display = "grid";
    input.value = "";

    await runSearchTurn(query, false);

    input.focus();
}

async function doFollowUp() {
    const query = followupInput.value.trim();
    if (!query || followupBtn.disabled) return;
    followupInput.value = "";
    await runSearchTurn(query, true);
    followupInput.focus();
}

async function runSearchTurn(query, isFollowUp) {
    setSearching(true);
    const { searching, answer, cursor } = addTurnToThread(query);
    threadEl.scrollIntoView({ behavior: "smooth", block: "end" });

    try {
        const res = await fetch("/search-chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ query, session_id: currentSessionId }),
        });
        if (!res.ok) throw new Error(`Error ${res.status}`);

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        let answerText = "";

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });

            let nl;
            while ((nl = buf.indexOf("\n")) !== -1) {
                const line = buf.slice(0, nl).trim();
                buf = buf.slice(nl + 1);
                if (!line) continue;
                let ev;
                try { ev = JSON.parse(line); } catch { continue; }

                switch (ev.e) {
                    case "thinking_start":
                        cursor.remove();
                        answer.appendChild(renderThinkBlock("", false));
                        answer.appendChild(cursor);
                        answer.scrollIntoView({ behavior: "smooth", block: "end" });
                        break;

                    case "thinking": {
                        const body = answer.querySelector(".think-body");
                        if (body) body.textContent += ev.d;
                        answer.scrollIntoView({ behavior: "smooth", block: "end" });
                        break;
                    }

                    case "thinking_end": {
                        const block = answer.querySelector(".think-block");
                        if (block) {
                            block.querySelector(".think-dot")?.classList.add("done");
                            const lbl = block.querySelector(".think-label");
                            if (lbl) lbl.textContent = "thought for a moment";
                        }
                        break;
                    }

                    case "searching":
                        updateSearchingLabel(searching, ev.d);
                        break;

                    case "sources":
                        appendSources(ev.d);
                        break;

                    case "text":
                        finalizeSearching(searching);
                        answerText += ev.d;
                        cursor.remove();
                        // Preserve any think block already rendered, append answer after it
                        const existingThink = answer.querySelector(".think-block");
                        if (existingThink) {
                            let contentEl = answer.querySelector(".turn-answer-content");
                            if (!contentEl) {
                                contentEl = document.createElement("div");
                                contentEl.className = "turn-answer-content";
                                answer.appendChild(contentEl);
                            }
                            contentEl.innerHTML = marked.parse(answerText);
                            contentEl.querySelectorAll("pre code").forEach(el => hljs.highlightElement(el));
                        } else {
                            answer.innerHTML = marked.parse(answerText);
                            answer.querySelectorAll("pre code").forEach(el => hljs.highlightElement(el));
                        }
                        answer.appendChild(cursor);
                        answer.scrollIntoView({ behavior: "smooth", block: "end" });
                        break;

                    case "done":
                        if (!currentSessionId) currentSessionId = ev.d.session_id;
                        break;

                    case "error":
                        finalizeSearching(searching);
                        cursor.remove();
                        answer.textContent = `Error: ${ev.d}`;
                        answer.style.color = "var(--red)";
                        break;
                }
            }
        }

        cursor.remove();
        // Final render pass — preserve think block if present
        if (answerText) {
            const existingThink = answer.querySelector(".think-block");
            if (existingThink) {
                let contentEl = answer.querySelector(".turn-answer-content");
                if (!contentEl) {
                    contentEl = document.createElement("div");
                    contentEl.className = "turn-answer-content";
                    answer.appendChild(contentEl);
                }
                contentEl.innerHTML = marked.parse(answerText);
                contentEl.querySelectorAll("pre code").forEach(el => hljs.highlightElement(el));
            } else {
                answer.innerHTML = marked.parse(answerText);
                answer.querySelectorAll("pre code").forEach(el => hljs.highlightElement(el));
            }
        }

        // Show follow-up input after first answer
        followupRow.style.display = "flex";
        setTimeout(() => loadHistory(), 1000);

    } catch (err) {
        finalizeSearching(searching);
        cursor.remove();
        answer.textContent = `Error: ${err.message}`;
        answer.style.color = "var(--red)";
    }

    setSearching(false);
}

// ── History ──────────────────────────────────────────────────
function toggleHistory() {
    const dd = document.getElementById("hist-dropdown");
    const chevron = document.getElementById("hist-chevron");
    const open = dd.classList.toggle("open");
    chevron.classList.toggle("open", open);
    if (open) loadHistory();
}

function closeHistory() {
    document.getElementById("hist-dropdown").classList.remove("open");
    document.getElementById("hist-chevron").classList.remove("open");
}

async function loadHistory() {
    try {
        const res = await fetch("/search-sessions");
        const sessions = await res.json();
        renderHistory(sessions);
    } catch {}
}

function renderHistory(sessions) {
    const listEl = document.getElementById("hist-list");
    if (!sessions.length) {
        listEl.innerHTML = '<div class="hist-empty">No searches yet</div>';
        return;
    }
    listEl.innerHTML = sessions.map(s => `
        <div class="hist-item ${s.id === currentSessionId ? "active" : ""}"
             onclick="loadSession('${s.id}')">
            <span class="hist-item-title">${escHtml(s.title)}</span>
            <button class="hist-delete" onclick="deleteSession(event,'${s.id}')" title="Delete">✕</button>
        </div>
    `).join("");
}

async function loadSession(id) {
    closeHistory();
    const res = await fetch(`/search-sessions/${id}/messages`);
    const msgs = await res.json();

    currentSessionId = id;
    sourceCount = 0;
    threadEl.innerHTML = "";
    sourcesListEl.innerHTML = "";
    followupRow.style.display = "none";
    grid.style.display = "grid";

    msgs.forEach(m => {
        if (m.role === "user") {
            const qEl = document.createElement("div");
            qEl.className = "turn-query";
            qEl.textContent = m.content;
            threadEl.appendChild(qEl);
        } else {
            appendSources(m.sources || []);
            const answer = document.createElement("div");
            answer.className = "turn-answer";
            answer.innerHTML = marked.parse(m.content);
            answer.querySelectorAll("pre code").forEach(el => hljs.highlightElement(el));
            threadEl.appendChild(answer);
            followupRow.style.display = "flex";
        }
    });
}

async function deleteSession(e, id) {
    e.stopPropagation();
    await fetch(`/search-sessions/${id}`, { method: "DELETE" });
    if (id === currentSessionId) newSearch();
    loadHistory();
}

function newSearch() {
    closeHistory();
    currentSessionId = null;
    sourceCount = 0;
    threadEl.innerHTML = "";
    sourcesListEl.innerHTML = '<div class="sources-empty">No sources yet</div>';
    followupRow.style.display = "none";
    grid.style.display = "none";
    input.value = "";
    input.focus();
}
