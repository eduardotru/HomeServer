// ── State ─────────────────────────────────────────────────────
let currentPath = null;       // currently open file path
let currentContent = null;    // raw text of open file
let isEditing = false;
let browsingPath = "";         // current directory in the sidebar

// ── Bootstrap ─────────────────────────────────────────────────
// Root the Notes UI at the `notes/` directory so files are always stored there.
const NOTES_ROOT = "notes";
loadTree(NOTES_ROOT);

// Handle browser back/forward and direct URL hash navigation
window.addEventListener("hashchange", () => {
    const p = decodeURIComponent(location.hash.slice(1));
    if (p) openFile(p);
});
if (location.hash) {
    const p = decodeURIComponent(location.hash.slice(1));
    if (p) openFile(p);
}

// ── Tree ───────────────────────────────────────────────────────
async function loadTree(path) {
    browsingPath = path;
    document.getElementById("sidebar-path").textContent = "/" + path;
    const treeEl = document.getElementById("tree");
    treeEl.innerHTML = '<div class="tree-empty">Loading…</div>';

    try {
        const url = path ? `/api/files/${path}` : "/api/files";
        const res = await fetch(url);
        if (!res.ok) throw new Error(res.statusText);
        const data = await res.json();
        renderTree(data.entries || [], path);
    } catch (e) {
        treeEl.innerHTML = `<div class="tree-empty">Error: ${escHtml(e.message)}</div>`;
    }
}

function renderTree(entries, currentDir) {
    const treeEl = document.getElementById("tree");
    if (!entries.length) {
        treeEl.innerHTML = '<div class="tree-empty">Empty</div>';
        return;
    }

    const dirs  = entries.filter(e => e.type === "directory").sort((a,b) => a.name.localeCompare(b.name));
    const files = entries.filter(e => e.type === "file").sort((a,b) => a.name.localeCompare(b.name));

    let html = "";

    // Back link when inside a subdirectory (but never above NOTES_ROOT)
    if (currentDir && currentDir !== NOTES_ROOT) {
        const parent = currentDir.includes("/")
            ? currentDir.slice(0, currentDir.lastIndexOf("/"))
            : NOTES_ROOT;
        html += `<div class="tree-item tree-back" onclick="loadTree('${escHtml(parent)}')">
            <span class="tree-icon">←</span><span class="tree-name">..</span>
        </div>`;
    }

    for (const d of dirs) {
        const fullPath = currentDir ? `${currentDir}/${d.name}` : d.name;
        html += `<div class="tree-item tree-dir" onclick="loadTree('${escHtml(fullPath)}')">
            <span class="tree-icon">▸</span>
            <span class="tree-name">${escHtml(d.name)}</span>
        </div>`;
    }

    for (const f of files) {
        const fullPath = currentDir ? `${currentDir}/${f.name}` : f.name;
        const active = fullPath === currentPath ? " active" : "";
        html += `<div class="tree-item tree-file${active}" onclick="openFile('${escHtml(fullPath)}')">
            <span class="tree-icon">${fileIcon(f.name)}</span>
            <span class="tree-name">${escHtml(f.name)}</span>
            <span class="tree-size">${fmtSize(f.size)}</span>
        </div>`;
    }

    treeEl.innerHTML = html;
}

function fileIcon(name) {
    if (name.endsWith(".md"))   return "◈";
    if (name.endsWith(".json")) return "{}";
    if (name.endsWith(".txt"))  return "≡";
    if (name.endsWith(".py"))   return "∿";
    if (name.endsWith(".js"))   return "⚡";
    return "·";
}

function fmtSize(bytes) {
    if (!bytes) return "";
    if (bytes < 1024) return bytes + "B";
    if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + "KB";
    return (bytes / (1024 * 1024)).toFixed(1) + "MB";
}

// ── Open / display file ────────────────────────────────────────
async function openFile(path) {
    if (isEditing && currentPath !== path) {
        if (!confirm("Discard unsaved changes?")) return;
        cancelEdit();
    }

    currentPath = path;
    location.hash = encodeURIComponent(path);

    // Highlight active in tree (re-render if same dir)
    document.querySelectorAll(".tree-file").forEach(el => el.classList.remove("active"));
    document.querySelectorAll(".tree-file").forEach(el => {
        if (el.querySelector(".tree-name")?.textContent === path.split("/").pop()) {
            el.classList.add("active");
        }
    });

    showFileView();
    document.getElementById("file-path").textContent = path;
    document.getElementById("file-body").innerHTML = '<div class="file-loading">Loading…</div>';

    try {
        const res = await fetch(`/api/file/${path}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        currentContent = await res.text();
        renderContent(path, currentContent);
    } catch (e) {
        document.getElementById("file-body").innerHTML =
            `<div class="file-error">Failed to load: ${escHtml(e.message)}</div>`;
    }
}

function renderContent(path, text) {
    const body = document.getElementById("file-body");
    if (path.endsWith(".md")) {
        body.innerHTML = `<div class="md-body">${marked.parse(text)}</div>`;
        body.querySelectorAll("pre code").forEach(el => hljs.highlightElement(el));
    } else {
        body.innerHTML = `<pre class="raw-body"><code>${escHtml(text)}</code></pre>`;
        hljs.highlightElement(body.querySelector("code"));
    }
}

// ── Edit ───────────────────────────────────────────────────────
function startEdit() {
    isEditing = true;
    const editor = document.getElementById("file-editor");
    editor.value = currentContent;
    editor.style.display = "block";
    document.getElementById("file-body").style.display = "none";
    document.getElementById("edit-btn").style.display = "none";
    document.getElementById("save-btn").style.display = "";
    document.getElementById("cancel-btn").style.display = "";
    editor.focus();
}

function cancelEdit() {
    isEditing = false;
    document.getElementById("file-editor").style.display = "none";
    document.getElementById("file-body").style.display = "";
    document.getElementById("edit-btn").style.display = "";
    document.getElementById("save-btn").style.display = "none";
    document.getElementById("cancel-btn").style.display = "none";
}

async function saveFile() {
    const content = document.getElementById("file-editor").value;
    const btn = document.getElementById("save-btn");
    btn.disabled = true;
    btn.textContent = "saving…";
    try {
        const res = await fetch(`/api/file/${currentPath}`, {
            method: "PUT",
            body: content,
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        currentContent = content;
        cancelEdit();
        renderContent(currentPath, currentContent);
        // Refresh tree to update size
        loadTree(browsingPath);
    } catch (e) {
        alert("Save failed: " + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = "save";
    }
}

// ── Delete ─────────────────────────────────────────────────────
async function deleteFile() {
    if (!currentPath) return;
    if (!confirm(`Delete "${currentPath}"?`)) return;
    try {
        const res = await fetch(`/api/file/${currentPath}`, { method: "DELETE" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        currentPath = null;
        currentContent = null;
        hideFileView();
        loadTree(browsingPath);
    } catch (e) {
        alert("Delete failed: " + e.message);
    }
}

// ── New file modal ─────────────────────────────────────────────
function newFile() {
    // Always prefill inside notes/ — at the notes root just use "notes/", deeper dirs include full path
    const prefill = browsingPath ? browsingPath + "/" : NOTES_ROOT + "/";
    document.getElementById("f-path").value = prefill;
    document.getElementById("f-content").value = "";
    document.getElementById("modal-backdrop").classList.add("visible");
    document.getElementById("modal").classList.add("visible");
    setTimeout(() => {
        const inp = document.getElementById("f-path");
        inp.focus();
        inp.setSelectionRange(inp.value.length, inp.value.length);
    }, 50);
}

function closeModal() {
    document.getElementById("modal-backdrop").classList.remove("visible");
    document.getElementById("modal").classList.remove("visible");
}

async function createFile(e) {
    e.preventDefault();
    const path = document.getElementById("f-path").value.trim().replace(/^\//, "");
    const content = document.getElementById("f-content").value;
    if (!path) return;
    try {
        const res = await fetch(`/api/file/${path}`, {
            method: "PUT",
            body: content,
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        closeModal();
        // Navigate tree to the file's parent dir
        const dir = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
        await loadTree(dir);
        await openFile(path);
    } catch (e) {
        alert("Create failed: " + e.message);
    }
}

// ── UI helpers ─────────────────────────────────────────────────
function showFileView() {
    document.getElementById("welcome").style.display = "none";
    document.getElementById("file-view").style.display = "flex";
}

function hideFileView() {
    document.getElementById("file-view").style.display = "none";
    document.getElementById("welcome").style.display = "flex";
}

// ── Keyboard ───────────────────────────────────────────────────
document.addEventListener("keydown", e => {
    if (e.key === "Escape") { closeModal(); if (isEditing) cancelEdit(); }
    if ((e.metaKey || e.ctrlKey) && e.key === "s" && isEditing) {
        e.preventDefault();
        saveFile();
    }
});
