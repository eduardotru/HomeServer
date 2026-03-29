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
let agentMode = false;
let alwaysAllow = false;
let abortController = null;

// ── Init ────────────────────────────────────────────────────
loadConversations();
loadModelStatus();

// Poll model status every 10s so the UI reflects swaps
setInterval(loadModelStatus, 10000);

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

// ── Agent mode ──────────────────────────────────────────────
function toggleAgent() {
    agentMode = !agentMode;
    document
        .getElementById("agent-toggle")
        .classList.toggle("on", agentMode);
    document
        .getElementById("toggle-track")
        .classList.toggle("on", agentMode);
    inputEl.placeholder = agentMode
        ? "Ask me to do something with the codebase..."
        : "Message...";

    // Show/hide always-allow toggle alongside agent mode
    const alwaysAllowEl = document.getElementById(
        "always-allow-toggle",
    );
    alwaysAllowEl.style.display = agentMode ? "flex" : "none";

    // Reset always-allow when agent mode is turned off
    if (!agentMode && alwaysAllow) {
        alwaysAllow = false;
        document
            .getElementById("always-allow-toggle")
            .classList.remove("on");
        document
            .getElementById("always-allow-track")
            .classList.remove("on");
    }

    // Update model tag — agent mode uses coder, regular uses main
    const modelTag = document.getElementById("model-tag");
    if (modelTag) {
        modelTag.textContent = agentMode
            ? "gpt-oss-20b"
            : "qwen3-8b";
    }
}

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
                    case "text":
                        // Full buffered text for this LLM turn — render as markdown
                        cursor.remove();
                        renderAssistantContent(bubble, ev.d);
                        scrollToBottom();
                        break;

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
                        if (!currentConvId && ev.d.conv_id) {
                            currentConvId = ev.d.conv_id;
                            convTitleEl.textContent = "New conversation";
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
      <div class="conv-item ${c.id === currentConvId ? "active" : ""}"
           onclick="loadConversation('${c.id}', ${JSON.stringify(c.title).replace(/"/g, "&quot;")})">
        <span class="conv-item-title">${escHtml(c.title)}</span>
        <span class="conv-item-date">${formatDate(c.created_at)}</span>
        <button class="conv-delete" onclick="deleteConversation(event, '${c.id}')" title="Delete">✕</button>
      </div>
    `,
        )
        .join("");
}

async function loadConversation(id, title) {
    closeDropdown();
    currentConvId = id;
    convTitleEl.textContent = title;
    messagesEl.innerHTML = "";
    const res = await fetch(`/conversations/${id}/messages`);
    const msgs = await res.json();
    msgs.forEach((m) => addMessage(m.role, m.content));
    renderConvList();
    inputEl.focus();
}

async function deleteConversation(e, id) {
    e.stopPropagation();
    await fetch(`/conversations/${id}`, { method: "DELETE" });
    if (id === currentConvId) newConversation();
    await loadConversations();
}

function newConversation() {
    closeDropdown();
    currentConvId = null;
    convTitleEl.textContent = "New conversation";
    messagesEl.innerHTML = "";
    messagesEl.appendChild(emptyStateEl());
    inputEl.focus();
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

function renderThinkBlock(thinking, done = true) {
    const block = document.createElement("div");
    block.className = "think-block";
    block.innerHTML = `
      <div class="think-header" onclick="this.closest('.think-block').classList.toggle('collapsed')">
        <div class="think-dot ${done ? "done" : ""}"></div>
        <span class="think-label">${done ? "thought for a moment" : "thinking…"}</span>
        <span class="think-chevron">▾</span>
      </div>
      <div class="think-body">${escHtml(thinking)}</div>
    `;
    return block;
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

    // ── Agent mode: use the server-side /agent loop ──────────
    if (agentMode) {
        try {
            const res = await fetch("/agent", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    prompt,
                    conversation_id: currentConvId,
                    always_allow: alwaysAllow,
                }),
                signal: abortController.signal,
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
            abortController = null;
            setGenerating(false);
            setStatus("ready");
            inputEl.focus();
        }
        return;
    }

    // ── Normal chat mode ─────────────────────────────────────
    try {
        const res = await fetch("/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                prompt,
                conversation_id: currentConvId,
                agent_mode: false,
            }),
            signal: abortController.signal,
        });

        if (!res.ok) throw new Error(`Error ${res.status}`);

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let text = "";

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            text += decoder.decode(value, { stream: true });

            // Extract conversation ID trailer
            const marker = "__CONV_ID__",
                end = "__END__";
            if (text.includes(marker)) {
                const idx = text.indexOf(marker),
                    eIdx = text.indexOf(end);
                if (eIdx !== -1) {
                    const newId = text.slice(
                        idx + marker.length,
                        eIdx,
                    );
                    if (!currentConvId) {
                        currentConvId = newId;
                        convTitleEl.textContent =
                            "New conversation";
                        loadConversations();
                    }
                    text = text.slice(0, idx);
                }
            }

            renderStreamingBubble(bubble, cursor, text);
            scrollToBottom(true);
        }

        cursor.remove();
        renderAssistantContent(bubble, text);
    } catch (err) {
        cursor.remove();
        if (err.name === "AbortError") {
            // Stopped by user — keep whatever was streamed
            if (bubble.textContent.trim()) {
                renderAssistantContent(bubble, bubble.textContent);
            } else {
                bubble.closest(".message")?.remove();
            }
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
