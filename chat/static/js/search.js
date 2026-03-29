const input = document.getElementById("search-input");
const searchBtn = document.getElementById("search-btn");
const brandDot = document.getElementById("brand-dot");
const statusEl = document.getElementById("status-text");
const grid = document.getElementById("results-grid");
const answerEl = document.getElementById("answer-body");
const sourcesEl = document.getElementById("sources-list");
const labelEl = document.getElementById("answer-label");

input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") doSearch();
});

function setSearching(active) {
    searchBtn.disabled = active;
    brandDot.className = "brand-dot" + (active ? " searching" : "");
    statusEl.textContent = active ? "searching…" : "";
}

function renderSources(results) {
    if (!results.length) {
        sourcesEl.innerHTML =
            '<div class="sources-empty">No sources found</div>';
        return;
    }
    sourcesEl.innerHTML = results
        .map(
            (r, i) => `
      <a class="source-item" href="${escHtml(r.url)}" target="_blank" rel="noopener">
        <span class="source-num">[${i + 1}]</span>
        <span class="source-title">${escHtml(r.title || r.url)}</span>
        <span class="source-url">${r.engine}: ${escHtml(hostname(r.url))}</span>
      </a>
    `,
        )
        .join("");
}

async function doSearch() {
    const query = input.value.trim();
    if (!query || searchBtn.disabled) return;

    setSearching(true);
    grid.style.display = "grid";
    answerEl.className = "answer-body empty";
    answerEl.textContent = "Searching…";
    sourcesEl.innerHTML =
        '<div class="sources-empty">Fetching results…</div>';
    labelEl.textContent = "AI ANSWER";

    try {
        const res = await fetch("/search-chat", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ query, stream: true }),
        });

        if (!res.ok) throw new Error(`Error ${res.status}`);

        // Parse search results from response header
        const rawResults = res.headers.get("X-Search-Results");
        if (rawResults) {
            try {
                renderSources(JSON.parse(rawResults));
            } catch {}
        }

        // Stream the answer
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let text = "";

        answerEl.className = "answer-body";
        answerEl.innerHTML = "";

        const cursor = document.createElement("span");
        cursor.className = "cursor";
        answerEl.appendChild(cursor);

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            text += decoder.decode(value, { stream: true });

            // Strip think blocks during streaming
            const thinkEnd = text.indexOf("</think>");
            const displayText =
                thinkEnd !== -1
                    ? text
                          .slice(thinkEnd + "</think>".length)
                          .trimStart()
                    : text.startsWith("<think>")
                      ? ""
                      : text;

            answerEl.textContent = displayText;
            answerEl.appendChild(cursor);
        }

        cursor.remove();

        // Final markdown render — strip think block first
        const thinkEnd = text.indexOf("</think>");
        const finalText =
            thinkEnd !== -1
                ? text
                      .slice(thinkEnd + "</think>".length)
                      .trimStart()
                : text.startsWith("<think>")
                  ? text
                  : text;

        answerEl.innerHTML = marked.parse(finalText);
        answerEl
            .querySelectorAll("pre code")
            .forEach((el) => hljs.highlightElement(el));
        labelEl.textContent = "AI ANSWER";
    } catch (err) {
        answerEl.textContent = `Error: ${err.message}`;
        answerEl.style.color = "var(--red)";
    }

    setSearching(false);
    input.focus();
}
