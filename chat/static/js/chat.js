const messagesEl = document.getElementById("messages");
const inputEl = document.getElementById("input");
const sendBtn = document.getElementById("send-btn");
const brandDot = document.getElementById("brand-dot");
const statusText = document.getElementById("status-text");
const convBtn = document.getElementById("conv-btn");
const convTitleEl = document.getElementById("conv-title");
const convChevron = document.getElementById("conv-chevron");
const convDropdown = document.getElementById("conv-dropdown");
const convList = document.getElementById("conv-list");

const stopBtn = document.getElementById("stop-btn");

let isGenerating = false;
let currentConvId = null;
let conversations = [];
let alwaysAllow = true;
let abortController = null;

// ── Init ────────────────────────────────────────────────────
loadConversations();
loadModelStatus();

// Poll model status every 10s so the UI reflects swaps
setInterval(loadModelStatus, 10000);

// Load conversation from URL on direct navigation (e.g. /chat/{uuid})
(function () {
    const m = location.pathname.match(/^\/chat\/([0-9a-f-]{36})$/i);
    if (m) loadConversation(m[1], false);
})();

// Handle browser back / forward
window.addEventListener("popstate", () => {
    const m = location.pathname.match(/^\/chat\/([0-9a-f-]{36})$/i);
    if (m) {
        loadConversation(m[1], false);
    } else {
        newConversation(false);
    }
});

async function loadModelStatus() {
    try {
        const res = await fetch("/api/models");
        if (!res.ok) return;
        const data = await res.json();
        const modelTag = document.getElementById("model-tag");
        if (modelTag && data.current) {
            const names = {
                main: "qwen3-8b",
                coder: "qwen2.5-coder",
            };
            modelTag.textContent =
                names[data.current] || data.current;
            modelTag.title =
                data.available[data.current]?.model || "";
        }
    } catch {}
}

document.addEventListener("click", (e) => {
    if (
        !convBtn.contains(e.target) &&
        !convDropdown.contains(e.target)
    )
        closeDropdown();
});

// ── Textarea auto-resize ────────────────────────────────────
inputEl.addEventListener("input", () => {
    inputEl.style.height = "auto";
    inputEl.style.height =
        Math.min(inputEl.scrollHeight, 160) + "px";
});

inputEl.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        if (!isGenerating) sendMessage();
    }
});

function fillPrompt(el) {
    inputEl.value = el.textContent;
    inputEl.dispatchEvent(new Event("input"));
    inputEl.focus();
}

// ── Always-allow toggle ─────────────────────────────────────
function toggleAlwaysAllow() {
    alwaysAllow = !alwaysAllow;
    document
        .getElementById("always-allow-toggle")
        .classList.toggle("on", alwaysAllow);
    document
        .getElementById("always-allow-track")
        .classList.toggle("on", alwaysAllow);
}

// ── Streaming bubble ────────────────────────────────────────
// Renders a bubble incrementally during streaming — updates in-place to
// avoid layout thrashing and scroll jumping from DOM rebuilds each token.
function renderStreamingBubble(bubble, cursor, text) {
    if (text.startsWith("<think>")) {
        const closeIdx = text.indexOf("</think>");

        if (closeIdx === -1) {
            // Still inside <think> — update existing think block or create one
            const thinking = text.slice("<think>".length);
            let block = bubble.querySelector(".think-block");
            if (!block) {
                bubble.innerHTML = "";
                block = renderThinkBlock("", false);
                bubble.appendChild(block);
            }
            // Update body text in-place
            const body = block.querySelector(".think-body");
            if (body) body.textContent = thinking;
            bubble.appendChild(cursor);
        } else {
            // Thinking done — finalise block and stream response below it
            const thinking = text.slice("<think>".length, closeIdx);
            const response = text
                .slice(closeIdx + "</think>".length)
                .trimStart();

            let block = bubble.querySelector(".think-block");
            if (!block) {
                bubble.innerHTML = "";
                block = renderThinkBlock(thinking, true);
                bubble.appendChild(block);
            } else {
                // Mark as done
                const dot = block.querySelector(".think-dot");
                const label = block.querySelector(".think-label");
                if (dot) {
                    dot.classList.add("done");
                }
                if (label) {
                    label.textContent = "thought for a moment";
                }
                const body = block.querySelector(".think-body");
                if (body) {
                    body.textContent = thinking;
                }
            }

            // Update or create the response node
            let responseEl =
                bubble.querySelector(".stream-response");
            if (!responseEl) {
                responseEl = document.createElement("div");
                responseEl.className = "stream-response";
                bubble.appendChild(responseEl);
            }
            responseEl.textContent = response;
            bubble.appendChild(cursor);
        }
    } else {
        // No think block — plain text stream
        let responseEl = bubble.querySelector(".stream-response");
        if (!responseEl) {
            // First token — clear any placeholder and create response node
            bubble.innerHTML = "";
            responseEl = document.createElement("div");
            responseEl.className = "stream-response";
            bubble.appendChild(responseEl);
        }
        responseEl.textContent = text;
        bubble.appendChild(cursor);
    }
}

// ── Tool card rendering ──────────────────────────────────────
function renderToolCard(toolCall) {
    const wrapper = document.createElement("div");
    wrapper.className = "tool-message";

    const label = document.createElement("div");
    label.className = "message-label";
    label.textContent = "tool";
    wrapper.appendChild(label);

    const card = document.createElement("div");
    card.className = "tool-card";

    const icons = {
        list_directory: "📁",
        read_file: "📄",
        write_file: "✏️",
        run_command: "⚡",
        web_search: "🔍",
    };

    const preview =
        toolCall.tool === "write_file"
            ? (toolCall.args?.content || "").slice(0, 400)
            : toolCall.tool === "run_command"
              ? toolCall.args?.command
              : toolCall.tool === "web_search"
                ? toolCall.args?.query
                : JSON.stringify(toolCall.args, null, 2);

    // token present means the server is waiting for confirmation
    const needsConfirm = !!toolCall.token;

    card.innerHTML = `
      <div class="tool-card-header">
        <span class="tool-icon">${icons[toolCall.tool] || "🔧"}</span>
        <span class="tool-card-name">${escHtml(toolCall.tool)}</span>
        <span class="tool-card-badge ${needsConfirm ? "destructive" : "safe"}">
          ${needsConfirm ? "confirm required" : "auto"}
        </span>
      </div>
      ${toolCall.reason ? `<div class="tool-card-reason">${escHtml(toolCall.reason)}</div>` : ""}
      <div class="tool-card-code">${escHtml(preview)}</div>
    `;

    card._toolCall = toolCall;
    wrapper.appendChild(card);
    return wrapper;
}

function showToolResult(card, result) {
    card.querySelector(".tool-result")?.remove();
    card.querySelector(".search-results")?.remove();

    const toolCall = card._toolCall;

    if (
        result.ok &&
        toolCall?.tool === "web_search" &&
        result.result?.results
    ) {
        // Render search results as source cards
        const container = document.createElement("div");
        container.className = "search-results";
        const results = result.result.results;
        if (results.length === 0) {
            container.innerHTML =
                '<div style="padding:8px 12px;font-size:11px;color:var(--text3);">No results found.</div>';
        } else {
            container.innerHTML = results
                .map(
                    (r, i) => `
          <a class="search-result-item" href="${escHtml(r.url)}" target="_blank" rel="noopener">
            <span class="search-result-num">[${i + 1}] ${escHtml(hostname(r.url))}</span>
            <span class="search-result-title">${escHtml(r.title || r.url)}</span>
            ${r.snippet ? `<span class="search-result-snippet">${escHtml(r.snippet)}</span>` : ""}
          </a>
        `,
                )
                .join("");
        }
        card.appendChild(container);
    } else {
        const div = document.createElement("div");
        div.className =
            "tool-result " + (result.ok ? "ok" : "error");
        const preview = result.ok
            ? JSON.stringify(result.result, null, 2).slice(0, 600)
            : result.error;
        div.textContent = preview;
        card.appendChild(div);
    }
    scrollToBottom();
}

// ── Confirm/reject destructive tools (server-side confirmation) ──
function addConfirmButtons(card, token) {
    const actions = document.createElement("div");
    actions.className = "tool-card-actions";
    actions.innerHTML = `
        <button class="tool-btn tool-btn-confirm" onclick="approveToolConfirm('${escHtml(token)}', this)">Run</button>
        <button class="tool-btn tool-btn-reject" onclick="rejectToolConfirm('${escHtml(token)}', this)">Cancel</button>
    `;
    card.appendChild(actions);
}

window.approveToolConfirm = async function (token, btn) {
    btn.closest(".tool-card-actions")
        .querySelectorAll(".tool-btn")
        .forEach((b) => (b.disabled = true));
    await fetch(`/agent/confirm/${token}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approved: true }),
    });
};

window.rejectToolConfirm = async function (token, btn) {
    btn.closest(".tool-card-actions")
        .querySelectorAll(".tool-btn")
        .forEach((b) => (b.disabled = true));
    await fetch(`/agent/confirm/${token}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approved: false }),
    });
};

// ── Agent loop ───────────────────────────────────────────────
// Reads the NDJSON event stream from /agent and drives the UI.
async function runAgentLoop(res, initBubble, initCursor) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let bubble = initBubble;
    let cursor = initCursor;
    let lastToolCard = null;

    try {
        loop: while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });

            // Process all complete newline-delimited JSON lines
            let nl;
            while ((nl = buf.indexOf("\n")) !== -1) {
                const line = buf.slice(0, nl).trim();
                buf = buf.slice(nl + 1);
                if (!line) continue;
                let ev;
                try {
                    ev = JSON.parse(line);
                } catch {
                    continue;
                }

                switch (ev.e) {
                    case "thinking_start":
                        // Start of a <think> block — render it live in the bubble
                        bubble.appendChild(renderThinkBlock("", false));
                        scrollToBottom();
                        break;

                    case "thinking": {
                        // Streamed chunk of thinking content
                        const body = bubble.querySelector(".think-body");
                        if (body) body.textContent += ev.d;
                        scrollToBottom();
                        break;
                    }

                    case "thinking_end": {
                        // <think> block finished — mark as done
                        const block = bubble.querySelector(".think-block");
                        if (block) {
                            block.querySelector(".think-dot")?.classList.add("done");
                            const lbl = block.querySelector(".think-label");
                            if (lbl) lbl.textContent = "thought for a moment";
                        }
                        scrollToBottom();
                        break;
                    }

                    case "text": {
                        // Full buffered text for this LLM turn — render as markdown.
                        // If thinking was already streamed into this bubble, preserve it
                        // and only append the response text.
                        cursor.remove();
                        const existingThink = bubble.querySelector(".think-block");
                        if (existingThink) {
                            if (ev.d.trim()) {
                                const content = document.createElement("div");
                                content.innerHTML = marked.parse(ev.d);
                                content.querySelectorAll("pre code").forEach(el => hljs.highlightElement(el));
                                bubble.appendChild(content);
                            }
                        } else {
                            renderAssistantContent(bubble, ev.d);
                        }
                        scrollToBottom();
                        break;
                    }

                    case "tool_start": {
                        const td = ev.d;
                        lastToolCard = renderToolCard(td);
                        messagesEl.appendChild(lastToolCard);
                        if (td.token) {
                            // Destructive tool — add confirm/cancel buttons
                            addConfirmButtons(
                                lastToolCard.querySelector(".tool-card"),
                                td.token,
                            );
                        }
                        scrollToBottom();
                        // Prepare a new bubble for the next LLM turn
                        bubble = addMessage("tool-feedback");
                        cursor = document.createElement("span");
                        cursor.className = "cursor";
                        bubble.appendChild(cursor);
                        break;
                    }

                    case "tool_done":
                        showToolResult(
                            lastToolCard?.querySelector(".tool-card"),
                            ev.d,
                        );
                        scrollToBottom();
                        break;

                    case "error":
                        cursor.remove();
                        bubble.textContent = `Error: ${ev.d}`;
                        bubble.style.color = "var(--red)";
                        break loop;

                    case "done":
                        cursor.remove();
                        // Remove bubble if empty (e.g. last turn was a tool call with no follow-up text)
                        if (!bubble.innerHTML.trim()) {
                            bubble.closest(".message")?.remove();
                        }
                        if (ev.d.conv_id) {
                            if (!currentConvId) {
                                currentConvId = ev.d.conv_id;
                                history.replaceState(
                                    { convId: ev.d.conv_id },
                                    "",
                                    `/chat/${ev.d.conv_id}`,
                                );
                            }
                            loadConversations();
                        }
                        break loop;
                }
            }
        }
    } catch (err) {
        cursor.remove();
        if (err.name !== "AbortError") {
            bubble.textContent = `Error: ${err.message}`;
            bubble.style.color = "var(--red)";
        } else if (!bubble.innerHTML.trim()) {
            bubble.closest(".message")?.remove();
        }
    }

    abortController = null;
    setGenerating(false);
    setStatus("ready");
    setTimeout(loadConversations, 2000);
    inputEl.focus();
}

function setGenerating(active) {
    isGenerating = active;
    sendBtn.disabled = active;
    stopBtn.classList.toggle("visible", active);
}

function stopGeneration() {
    if (abortController) {
        abortController.abort();
        abortController = null;
    }
}

function setStatus(state) {
    brandDot.className =
        "brand-dot" + (state === "thinking" ? " thinking" : "");
    statusText.textContent =
        state === "thinking" ? "generating…" : "";
}

// ── Dropdown ────────────────────────────────────────────────
function toggleDropdown() {
    const open = convDropdown.classList.toggle("open");
    convChevron.classList.toggle("open", open);
    if (open) loadConversations();
}

function closeDropdown() {
    convDropdown.classList.remove("open");
    convChevron.classList.remove("open");
}

async function loadConversations() {
    try {
        const res = await fetch("/conversations");
        conversations = await res.json();
        renderConvList();
    } catch (e) {
        console.error("Failed to load conversations", e);
    }
}

function renderConvList() {
    if (conversations.length === 0) {
        convList.innerHTML =
            '<div class="conv-empty">No conversations yet</div>';
        return;
    }
    convList.innerHTML = conversations
        .map(
            (c) => `
      <a class="conv-item ${c.id === currentConvId ? "active" : ""}"
         href="/chat/${c.id}"
         data-id="${c.id}">
        <span class="conv-item-title">${escHtml(c.title)}</span>
        <span class="conv-item-date">${formatDate(c.created_at)}</span>
        <button class="conv-delete" data-delete-id="${c.id}" title="Delete">✕</button>
      </a>
    `,
        )
        .join("");

    convList.querySelectorAll("a.conv-item").forEach((el) => {
        el.addEventListener("click", (e) => {
            const delBtn = e.target.closest(".conv-delete");
            if (delBtn) {
                e.preventDefault();
                e.stopPropagation();
                deleteConversation(e, delBtn.dataset.deleteId);
                return;
            }
            e.preventDefault();
            loadConversation(el.dataset.id);
        });
    });
}

async function loadConversation(id, pushHistory = true) {
    closeDropdown();
    currentConvId = id;
    if (pushHistory) history.pushState({ convId: id }, "", `/chat/${id}`);

    const cached = conversations.find((c) => c.id === id);
    convTitleEl.textContent = cached?.title || "…";

    messagesEl.innerHTML = "";
    document.getElementById("empty-state")?.remove();

    try {
        const res = await fetch(`/conversations/${id}/messages`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const msgs = await res.json();
        replayMessages(msgs);
        scrollToBottom(true);
    } catch (e) {
        addMessage("assistant", `Error loading conversation: ${e.message}`);
    }

    await loadConversations();
    const fresh = conversations.find((c) => c.id === id);
    if (fresh) convTitleEl.textContent = fresh.title;
    inputEl.focus();
}

async function deleteConversation(e, id) {
    e.stopPropagation();
    await fetch(`/conversations/${id}`, { method: "DELETE" });
    if (id === currentConvId) newConversation();
    await loadConversations();
}

function newConversation(pushHistory = true) {
    closeDropdown();
    if (pushHistory) history.pushState(null, "", "/");
    currentConvId = null;
    convTitleEl.textContent = "New conversation";
    messagesEl.innerHTML = "";
    messagesEl.appendChild(emptyStateEl());
    inputEl.focus();
}

// ── Replay stored messages into the DOM ─────────────────────
// Structured rows: kind in {user, assistant, tool_call, tool_result}.
// tool_call adds a tool card; the following tool_result fills it in.
function replayMessages(msgs) {
    let pendingCard = null;
    msgs.forEach((m) => {
        const kind = m.kind || m.role;
        const meta = m.metadata || {};
        switch (kind) {
            case "user":
                pendingCard = null;
                addMessage("user", m.content || "");
                break;
            case "assistant":
                pendingCard = null;
                if ((m.content || "").trim()) addMessage("assistant", m.content);
                break;
            case "tool_call": {
                pendingCard = renderToolCard({
                    tool: meta.tool || "tool",
                    args: meta.args || {},
                    reason: meta.reason || "",
                    destructive: !!meta.destructive,
                    token: null, // historical — no live confirmation
                });
                messagesEl.appendChild(pendingCard);
                break;
            }
            case "tool_result": {
                let card = pendingCard?.querySelector(".tool-card");
                if (!card) {
                    // Orphan (legacy pre-migration row or missing tool_call) —
                    // render a minimal card so the result still has a home.
                    const wrapper = renderToolCard({
                        tool: meta.tool || "tool",
                        args: {},
                        reason: "",
                        token: null,
                    });
                    messagesEl.appendChild(wrapper);
                    card = wrapper.querySelector(".tool-card");
                }
                showToolResult(card, meta);
                pendingCard = null;
                break;
            }
            default:
                // Unknown kind — fall back to plain text so we never drop content.
                if ((m.content || "").trim()) addMessage(m.role || "assistant", m.content);
        }
    });
}

function emptyStateEl() {
    const div = document.createElement("div");
    div.className = "empty-state";
    div.id = "empty-state";
    div.innerHTML = `
      <span class="empty-label">local · private · on-device</span>
      <div class="suggestions">
        <span class="suggestion" onclick="fillPrompt(this)">Explain MLX</span>
        <span class="suggestion" onclick="fillPrompt(this)">Write a Python quicksort</span>
        <span class="suggestion" onclick="fillPrompt(this)">What is Apple Silicon?</span>
        <span class="suggestion" onclick="fillPrompt(this)">FastAPI vs Flask</span>
      </div>`;
    return div;
}

// ── Thinking block ──────────────────────────────────────────
function parseThinkBlock(text) {
    const match = text.match(/^<think>([\s\S]*?)<\/think>\s*/);
    if (!match) return { thinking: null, response: text };
    return {
        thinking: match[1].trim(),
        response: text.slice(match[0].length),
    };
}


function renderAssistantContent(bubble, text) {
    const { thinking, response } = parseThinkBlock(text);
    bubble.innerHTML = "";
    if (thinking !== null) {
        bubble.appendChild(renderThinkBlock(thinking, true));
    }
    if (response.trim()) {
        const content = document.createElement("div");
        content.innerHTML = marked.parse(response);
        content
            .querySelectorAll("pre code")
            .forEach((el) => hljs.highlightElement(el));
        bubble.appendChild(content);
    }
}

// ── Messages ────────────────────────────────────────────────
function addMessage(role, content = "") {
    document.getElementById("empty-state")?.remove();
    const msg = document.createElement("div");
    msg.className = `message ${role}`;
    const label = document.createElement("div");
    label.className = "message-label";
    label.textContent =
        role === "user"
            ? "you"
            : role === "tool-feedback"
              ? "agent"
              : "ai";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    if (
        (role === "assistant" || role === "tool-feedback") &&
        content
    ) {
        renderAssistantContent(bubble, content);
    } else {
        bubble.textContent = content;
    }
    msg.appendChild(label);
    msg.appendChild(bubble);
    messagesEl.appendChild(msg);
    scrollToBottom();
    return bubble;
}

function scrollToBottom(instant = false) {
    messagesEl.scrollTo({
        top: messagesEl.scrollHeight,
        behavior: instant ? "instant" : "smooth",
    });
}

// ── Send ────────────────────────────────────────────────────
async function sendMessage() {
    const prompt = inputEl.value.trim();
    if (!prompt || isGenerating) return;

    setGenerating(true);
    setStatus("thinking");
    inputEl.value = "";
    inputEl.style.height = "auto";

    addMessage("user", prompt);
    const bubble = addMessage("assistant");
    const cursor = document.createElement("span");
    cursor.className = "cursor";
    bubble.appendChild(cursor);

    abortController = new AbortController();

    try {
        const timeoutSignal = AbortSignal.timeout
            ? AbortSignal.timeout(600_000)
            : null;
        const signal = timeoutSignal
            ? AbortSignal.any
                ? AbortSignal.any([abortController.signal, timeoutSignal])
                : abortController.signal
            : abortController.signal;
        const res = await fetch("/agent", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                prompt,
                conversation_id: currentConvId,
                always_allow: alwaysAllow,
            }),
            signal,
        });
        if (!res.ok) throw new Error(`Error ${res.status}`);
        await runAgentLoop(res, bubble, cursor);
    } catch (err) {
        cursor.remove();
        if (err.name === "AbortError") {
            if (!bubble.innerHTML.trim())
                bubble.closest(".message")?.remove();
        } else {
            bubble.textContent = `Error: ${err.message}`;
            bubble.style.color = "var(--red)";
            bubble.style.borderColor = "var(--red)";
        }
    }

    abortController = null;
    setGenerating(false);
    setStatus("ready");
    setTimeout(loadConversations, 2000);
    inputEl.focus();
}

// ── Helpers ─────────────────────────────────────────────────
function formatDate(iso) {
    return new Date(iso).toLocaleDateString("en-GB", {
        day: "2-digit",
        month: "short",
    });
}
