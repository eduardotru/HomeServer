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

function escHtml(s) {
    return (s || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
}

function hostname(url) {
    try {
        return new URL(url).hostname;
    } catch {
        return url;
    }
}

marked.setOptions({
    breaks: true,
    gfm: true,
    highlight: (code, lang) => {
        if (lang && hljs.getLanguage(lang))
            return hljs.highlight(code, { language: lang }).value;
        return hljs.highlightAuto(code).value;
    },
});
