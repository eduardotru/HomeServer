// HomeServer UI kit — vanilla web components, no build step.
// Components use light DOM so apps can style and query freely. This is
// a deliberate trade-off: zero isolation in exchange for simple debugging
// and the ability to use the component's children as regular DOM.

class HsCard extends HTMLElement {}
customElements.define("hs-card", HsCard);

class HsButton extends HTMLElement {
    connectedCallback() {
        if (this.querySelector(":scope > button")) return;
        const btn = document.createElement("button");
        btn.type = this.getAttribute("type") || "button";
        if (this.hasAttribute("disabled")) btn.disabled = true;
        while (this.firstChild) btn.appendChild(this.firstChild);
        this.appendChild(btn);
        btn.addEventListener("click", (e) => {
            if (this.hasAttribute("disabled")) {
                e.stopPropagation();
                e.preventDefault();
                return;
            }
            this.dispatchEvent(new CustomEvent("hs-click", { bubbles: true }));
        });
    }
}
customElements.define("hs-button", HsButton);

class HsInput extends HTMLElement {
    connectedCallback() {
        if (this.querySelector(":scope > input, :scope > textarea")) return;
        const tag = this.getAttribute("multiline") !== null ? "textarea" : "input";
        const el = document.createElement(tag);
        for (const attr of [
            "type",
            "name",
            "placeholder",
            "value",
            "min",
            "max",
            "step",
            "pattern",
            "required",
            "rows",
        ]) {
            if (this.hasAttribute(attr)) el.setAttribute(attr, this.getAttribute(attr));
        }
        this.appendChild(el);
    }
    get value() {
        return this.querySelector("input, textarea")?.value ?? "";
    }
    set value(v) {
        const el = this.querySelector("input, textarea");
        if (el) el.value = v;
    }
}
customElements.define("hs-input", HsInput);

class HsField extends HTMLElement {
    connectedCallback() {
        if (this.querySelector(":scope > label")) return;
        const label = this.getAttribute("label");
        if (label) {
            const l = document.createElement("label");
            l.textContent = label;
            this.insertBefore(l, this.firstChild);
        }
    }
}
customElements.define("hs-field", HsField);

class HsForm extends HTMLElement {
    connectedCallback() {
        this.addEventListener("submit", (e) => e.preventDefault());
        this.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && e.target.tagName === "INPUT") {
                e.preventDefault();
                this._submit();
            }
        });
        const submitBtn = this.querySelector('hs-button[type="submit"]');
        if (submitBtn) {
            submitBtn.addEventListener("hs-click", () => this._submit());
        }
    }
    _submit() {
        const data = {};
        for (const input of this.querySelectorAll("hs-input, input, textarea, select")) {
            const name = input.getAttribute("name");
            if (!name) continue;
            data[name] = input.value;
        }
        this.dispatchEvent(
            new CustomEvent("hs-submit", { detail: data, bubbles: true })
        );
    }
    get values() {
        const data = {};
        for (const input of this.querySelectorAll("hs-input, input, textarea, select")) {
            const name = input.getAttribute("name");
            if (!name) continue;
            data[name] = input.value;
        }
        return data;
    }
    reset() {
        for (const input of this.querySelectorAll("hs-input, input, textarea, select")) {
            if ("value" in input) input.value = "";
        }
    }
}
customElements.define("hs-form", HsForm);

class HsList extends HTMLElement {
    render(items, renderItem) {
        const ul = document.createElement("ul");
        for (const it of items) {
            const li = document.createElement("li");
            const content = renderItem(it);
            if (typeof content === "string") li.innerHTML = content;
            else li.appendChild(content);
            ul.appendChild(li);
        }
        this.replaceChildren(ul);
    }
}
customElements.define("hs-list", HsList);

class HsTable extends HTMLElement {
    render(columns, rows) {
        const table = document.createElement("table");
        const thead = document.createElement("thead");
        const tr = document.createElement("tr");
        for (const c of columns) {
            const th = document.createElement("th");
            th.textContent = c.label ?? c.key;
            tr.appendChild(th);
        }
        thead.appendChild(tr);
        table.appendChild(thead);
        const tbody = document.createElement("tbody");
        for (const r of rows) {
            const trb = document.createElement("tr");
            for (const c of columns) {
                const td = document.createElement("td");
                const v = c.render ? c.render(r) : r[c.key];
                if (v instanceof Node) td.appendChild(v);
                else td.textContent = v ?? "";
                trb.appendChild(td);
            }
            tbody.appendChild(trb);
        }
        table.appendChild(tbody);
        this.replaceChildren(table);
    }
}
customElements.define("hs-table", HsTable);

class HsUpload extends HTMLElement {
    connectedCallback() {
        if (this.querySelector(":scope > label")) return;
        const label = document.createElement("label");
        const text = document.createElement("span");
        text.textContent = this.getAttribute("label") || "Choose file…";
        const input = document.createElement("input");
        input.type = "file";
        if (this.hasAttribute("accept")) input.accept = this.getAttribute("accept");
        if (this.hasAttribute("multiple")) input.multiple = true;
        input.addEventListener("change", () => {
            this.dispatchEvent(
                new CustomEvent("hs-files", {
                    detail: { files: Array.from(input.files) },
                    bubbles: true,
                })
            );
        });
        label.append(text, input);
        this.appendChild(label);
    }
}
customElements.define("hs-upload", HsUpload);

class HsEmpty extends HTMLElement {}
customElements.define("hs-empty", HsEmpty);

// --- Clients exposed to apps -------------------------------------------------

export const api = {
    async get(path) {
        const r = await fetch(path);
        if (!r.ok) throw new Error(`GET ${path} → ${r.status}`);
        return r.json();
    },
    async post(path, body) {
        const r = await fetch(path, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify(body ?? {}),
        });
        if (!r.ok) throw new Error(`POST ${path} → ${r.status}`);
        return r.json();
    },
    async del(path) {
        const r = await fetch(path, { method: "DELETE" });
        if (!r.ok) throw new Error(`DELETE ${path} → ${r.status}`);
        return r.json();
    },
};

// Broadcast that something meaningful happened so the chat sidebar (parent frame)
// can keep the AI in the loop. Fire-and-forget; no-op outside an iframe.
export function notifyHost(type, detail) {
    if (window.parent === window) return;
    window.parent.postMessage({ hsEvent: type, detail }, "*");
}

window.hs = { api, notifyHost };
