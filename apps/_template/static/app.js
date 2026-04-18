// Opinionated app template. Copy this pattern.
//
// Key ideas:
//   - APP_BASE scopes fetches to this app's mount point so the same code
//     works whether the app is served standalone or inside the chat wrapper.
//   - Use <hs-form>'s `hs-submit` event instead of wiring "click" on buttons.
//   - Use <hs-list>.render() to paint lists — it handles DOM diffing for you.
//   - notifyHost() tells the parent frame (chat) that data changed so the AI
//     can see the effect of its actions.

import { api, notifyHost } from "/ui/kit.js";

const APP_BASE = new URL("..", window.location.href).pathname.replace(/\/$/, "");
const API = (p) => `${APP_BASE}/api${p}`;

const form = document.getElementById("add-form");
const list = document.getElementById("items");

async function refresh() {
    const items = await api.get(API("/items"));
    list.render(items, (it) => {
        const row = document.createElement("div");
        row.className = "row";
        const name = document.createElement("span");
        name.textContent = it.name;
        const del = document.createElement("button");
        del.textContent = "×";
        del.className = "icon";
        del.addEventListener("click", async () => {
            await api.del(API(`/items/${it.id}`));
            notifyHost("items.changed");
            refresh();
        });
        row.append(name, del);
        return row;
    });
}

form.addEventListener("hs-submit", async (e) => {
    const { name } = e.detail;
    if (!name?.trim()) return;
    await api.post(API("/items"), { name });
    form.reset();
    notifyHost("items.changed", { name });
    refresh();
});

refresh();
