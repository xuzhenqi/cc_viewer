// ---------------------------------------------------------------------------
// DOM refs
// ---------------------------------------------------------------------------
const listEl = document.getElementById("list");
const emptyEl = document.getElementById("empty");
const detailEl = document.getElementById("detail");
const totalEl = document.getElementById("total");
const searchEl = document.getElementById("search");
const refreshBtn = document.getElementById("refresh");
const autoEl = document.getElementById("auto");
const modeCompact = document.getElementById("modeCompact");
const modeRaw = document.getElementById("modeRaw");
const sysBtn = document.getElementById("sysBtn");
const catBtn = document.getElementById("catBtn");

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let mode = "dumps";            // "dumps" | "systems" | "catalog" | "prompt"
let viewMode = "compact";      // "compact" | "raw"
let promptName = null;         // current prompt name when mode === "prompt"
let allItems = [];
let allSystems = [];
let catalogEntries = [];       // [{hash, name, text, created_at, updated_at}]
let catalogByHash = new Map(); // hash -> entry
let selectedFile = null;
let selectedSession = null;
let selectedCatalogHash = null;
let autoTimer = null;
const collapsedSessions = new Set();
const NO_SESSION = "(no session)";

// ---------------------------------------------------------------------------
// Generic helpers
// ---------------------------------------------------------------------------
function fmtTime(ts) {
    if (!ts) return "";
    const d = new Date(ts);
    if (isNaN(d)) return ts;
    return d.toLocaleTimeString([], { hour12: false });
}

function fmtSize(n) {
    if (n == null) return "";
    if (n < 1024) return n + "B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + "K";
    return (n / (1024 * 1024)).toFixed(1) + "M";
}

function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[c]);
}

function preview(text, n) {
    if (!text) return "";
    text = text.replace(/\s+/g, " ").trim();
    return text.length > n ? text.slice(0, n) + "…" : text;
}

// ---------------------------------------------------------------------------
// Classifier (JS port of classify.py — regex set is the same)
// ---------------------------------------------------------------------------
const RE_SYS_REMINDER = /^\s*<system-reminder>/;
const RE_CMD_TAG = /^\s*<command-(?:message|name|args)>/;
const RE_LOCAL_CMD = /^\s*<local-command-(?:stdout|stderr)>/;
const RE_CONTINUATION = /^This session is being continued from a previous conversation/;
const RE_ENV_HEADER = /^# (?:claudeMd|currentDate|Environment|auto memory)\b/;

const KIND = {
    SYSTEM_REMINDER: "system_reminder",
    COMMAND_META: "command_meta",
    LOCAL_COMMAND: "local_command",
    CONTINUATION: "continuation",
    ENVIRONMENT_CONTEXT: "environment_context",
    USER_TEXT: "user_text",
    TOOL_USE: "tool_use",
    TOOL_RESULT: "tool_result",
    THINKING: "thinking",
    IMAGE: "image",
    UNKNOWN: "unknown",
};

const SYSTEM_KINDS = new Set([
    KIND.SYSTEM_REMINDER, KIND.COMMAND_META, KIND.LOCAL_COMMAND,
    KIND.CONTINUATION, KIND.ENVIRONMENT_CONTEXT,
]);

const KIND_LABEL = {
    [KIND.SYSTEM_REMINDER]: "system-reminder",
    [KIND.COMMAND_META]: "command",
    [KIND.LOCAL_COMMAND]: "local-command",
    [KIND.CONTINUATION]: "continuation",
    [KIND.ENVIRONMENT_CONTEXT]: "environment",
};

function classifyText(text) {
    if (!text) return KIND.UNKNOWN;
    if (RE_SYS_REMINDER.test(text)) return KIND.SYSTEM_REMINDER;
    const stripped = text.replace(/^\s+/, "");
    if (RE_CMD_TAG.test(stripped)) return KIND.COMMAND_META;
    if (RE_LOCAL_CMD.test(stripped)) return KIND.LOCAL_COMMAND;
    if (RE_CONTINUATION.test(stripped)) return KIND.CONTINUATION;
    if (RE_ENV_HEADER.test(stripped)) return KIND.ENVIRONMENT_CONTEXT;
    return KIND.USER_TEXT;
}

function classifyBlock(b) {
    if (!b || typeof b !== "object") return KIND.UNKNOWN;
    const t = b.type;
    if (t === "text") return classifyText(b.text);
    if (t === "tool_use") return KIND.TOOL_USE;
    if (t === "tool_result") return KIND.TOOL_RESULT;
    if (t === "thinking") return KIND.THINKING;
    if (t === "image") return KIND.IMAGE;
    return KIND.UNKNOWN;
}

function blockText(b) {
    const t = b.type;
    if (t === "text") return b.text || "";
    if (t === "thinking") return b.thinking || "";
    if (t === "tool_use") {
        return "[tool_use: " + (b.name || "?") + "] " + JSON.stringify(b.input || {});
    }
    if (t === "tool_result") {
        const c = b.content;
        if (typeof c === "string") return c;
        if (Array.isArray(c)) return c.map(x => blockText(x)).join("\n");
        if (c == null) return "";
        return JSON.stringify(c);
    }
    if (t === "image") return "[image]";
    return JSON.stringify(b);
}

function classifyMessage(msg) {
    const content = msg && msg.content;
    const blocks = [];
    if (typeof content === "string") {
        blocks.push({ raw: { type: "text", text: content }, kind: classifyText(content), text: content });
    } else if (Array.isArray(content)) {
        for (const b of content) {
            if (!b || typeof b !== "object") continue;
            blocks.push({ raw: b, kind: classifyBlock(b), text: blockText(b) });
        }
    }
    return { role: (msg && msg.role) || "", blocks };
}

// ---------------------------------------------------------------------------
// Prompt catalog annotation
// ---------------------------------------------------------------------------
async function sha256Hex(str) {
    const buf = new TextEncoder().encode(str);
    const digest = await crypto.subtle.digest("SHA-256", buf);
    return Array.from(new Uint8Array(digest))
        .map(b => b.toString(16).padStart(2, "0")).join("");
}

// Cached sha256 promises so repeated annotation of the same string reuses work.
const _hashCache = new Map();
async function sha256HexCached(str) {
    if (_hashCache.has(str)) return _hashCache.get(str);
    const p = sha256Hex(str);
    _hashCache.set(str, p);
    return p;
}

async function annotateSystemPrompt(systemField, catalogByHash) {
    // Normalize to array of strings (one per "unit"). A string field is one
    // unit; an array of content blocks contributes each {type:"text"} element.
    let texts = [];
    if (typeof systemField === "string") {
        if (systemField) texts.push(systemField);
    } else if (Array.isArray(systemField)) {
        for (const b of systemField) {
            if (b && b.type === "text" && b.text) texts.push(b.text);
        }
    }
    const out = [];
    for (const text of texts) {
        const hex = await sha256HexCached(text);
        const hash = "sha256:" + hex;
        out.push({ text, hash, entry: catalogByHash.get(hash) || null });
    }
    return out;
}

// ---------------------------------------------------------------------------
// Compact-message rendering
// ---------------------------------------------------------------------------
function messageStats(m) {
    const s = { user: 0, system: 0, tool_use: 0, tool_result: 0, thinking: 0, image: 0 };
    for (const b of m.blocks) {
        if (b.kind === KIND.USER_TEXT) s.user++;
        else if (SYSTEM_KINDS.has(b.kind)) s.system++;
        else if (b.kind === KIND.TOOL_USE) s.tool_use++;
        else if (b.kind === KIND.TOOL_RESULT) s.tool_result++;
        else if (b.kind === KIND.THINKING) s.thinking++;
        else if (b.kind === KIND.IMAGE) s.image++;
    }
    return s;
}

function statsLabel(s) {
    const parts = [];
    if (s.user) parts.push(s.user + " user text");
    if (s.system) parts.push(s.system + " system");
    if (s.tool_use) parts.push(s.tool_use + " tool_use");
    if (s.tool_result) parts.push(s.tool_result + " tool_result");
    if (s.thinking) parts.push(s.thinking + " thinking");
    if (s.image) parts.push(s.image + " image");
    return parts.join(" · ");
}

function renderSystemPromptPseudo(units, onAddToCatalog) {
    const det = document.createElement("details");
    det.className = "msg msg-system sys-prompt";
    det.open = false;
    const totalChars = units.reduce((s, u) => s + u.text.length, 0);
    const matched = units.filter(u => u.entry).length;
    const summary = document.createElement("summary");
    summary.className = "msg-summary";
    summary.innerHTML =
        '<span class="role-badge role-system">system</span>' +
        '<span class="msg-idx">prompt</span>' +
        '<span class="msg-stats">' + esc(
            units.length + " block" + (units.length === 1 ? "" : "s") +
            " · " + matched + " named · " + totalChars + " chars") + '</span>';
    det.appendChild(summary);
    for (const u of units) {
        const wrap = document.createElement("div");
        wrap.className = "sys-prompt-block" + (u.entry ? " matched" : " unmatched");
        const badge = document.createElement("span");
        badge.className = "cat-badge" + (u.entry ? "" : " ghost");
        badge.textContent = u.entry ? u.entry.name : "unnamed";
        if (u.entry) {
            badge.title = u.entry.hash;
            badge.addEventListener("click", (e) => {
                e.stopPropagation();
                // Deep-link to the prompt page; the SPA router will load
                // the prompt view and push the URL.
                navigateToPrompt(u.entry.name);
            });
        }
        wrap.appendChild(badge);
        if (u.entry) {
            const pre = document.createElement("pre");
            pre.textContent = u.text;
            wrap.appendChild(pre);
        } else {
            const preview = document.createElement("div");
            preview.className = "sys-prompt-unmatched";
            const pre = document.createElement("pre");
            pre.textContent = u.text;
            preview.appendChild(pre);
            const addBtn = document.createElement("button");
            addBtn.className = "button cat-add";
            addBtn.textContent = "Add to catalog";
            addBtn.addEventListener("click", (e) => {
                e.stopPropagation();
                onAddToCatalog(u);
            });
            preview.appendChild(addBtn);
            wrap.appendChild(preview);
        }
        det.appendChild(wrap);
    }
    return det;
}

function renderMessageCard(cls, idx) {
    const s = messageStats(cls);
    const card = document.createElement("div");
    card.className = "msg msg-" + (cls.role || "unknown");

    const header = document.createElement("div");
    header.className = "msg-header";
    const role = document.createElement("span");
    role.className = "role-badge role-" + (cls.role || "unknown");
    role.textContent = cls.role || "?";
    header.appendChild(role);
    const idxLabel = document.createElement("span");
    idxLabel.className = "msg-idx";
    idxLabel.textContent = "#" + idx;
    header.appendChild(idxLabel);
    const stats = document.createElement("span");
    stats.className = "msg-stats";
    stats.textContent = statsLabel(s);
    header.appendChild(stats);
    card.appendChild(header);

    const body = document.createElement("div");
    body.className = "msg-body";
    for (const b of cls.blocks) {
        body.appendChild(renderBlock(b));
    }
    card.appendChild(body);
    return card;
}

function renderBlock(b) {
    if (SYSTEM_KINDS.has(b.kind)) return renderCollapsibleSystem(b);
    if (b.kind === KIND.TOOL_USE) return renderToolUse(b);
    if (b.kind === KIND.TOOL_RESULT) return renderToolResult(b);
    if (b.kind === KIND.THINKING) return renderThinking(b);
    if (b.kind === KIND.IMAGE) return renderImage(b);
    if (b.kind === KIND.USER_TEXT) return renderUserText(b);
    return renderGeneric(b);
}

function renderCollapsibleSystem(b) {
    const det = document.createElement("details");
    det.className = "sys-block kind-" + b.kind;
    det.open = false;
    const summary = document.createElement("summary");
    const label = KIND_LABEL[b.kind] || b.kind;
    summary.innerHTML =
        '<span class="kind-label">' + esc(label) + '</span>' +
        '<span class="kind-preview">' + esc(preview(b.text, 80)) + '</span>';
    det.appendChild(summary);
    const pre = document.createElement("pre");
    pre.textContent = b.text || "";
    det.appendChild(pre);
    return det;
}

function renderToolUse(b) {
    const det = document.createElement("details");
    det.className = "sys-block kind-tool-use";
    det.open = false;
    const inputKeys = Object.keys((b.raw && b.raw.input) || {});
    const summary = document.createElement("summary");
    summary.innerHTML =
        '<span class="kind-label">tool_use</span>' +
        '<span class="kind-preview">' + esc((b.raw && b.raw.name) || "?") +
        "(" + esc(inputKeys.join(", ") || "—") + ")</span>";
    det.appendChild(summary);
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(b.raw && b.raw.input, null, 2);
    det.appendChild(pre);
    return det;
}

function renderToolResult(b) {
    const det = document.createElement("details");
    det.className = "sys-block kind-tool-result";
    det.open = false;
    const raw = b.raw || {};
    const tuid = raw.tool_use_id || "";
    const isError = raw.is_error === true;
    const content = raw.content;
    let len = 0;
    if (typeof content === "string") len = content.length;
    else if (Array.isArray(content)) len = JSON.stringify(content).length;
    const summary = document.createElement("summary");
    summary.innerHTML =
        '<span class="kind-label">tool_result' + (isError ? " error" : "") + '</span>' +
        '<span class="kind-preview">' + esc(tuid.slice(0, 24)) +
        " · " + esc(String(len)) + " chars</span>";
    det.appendChild(summary);
    const pre = document.createElement("pre");
    if (typeof content === "string") pre.textContent = content;
    else pre.textContent = JSON.stringify(content, null, 2);
    det.appendChild(pre);
    return det;
}

function renderThinking(b) {
    const det = document.createElement("details");
    det.className = "sys-block kind-thinking";
    det.open = false;
    const text = (b.raw && b.raw.thinking) || "";
    const summary = document.createElement("summary");
    summary.innerHTML =
        '<span class="kind-label">thinking</span>' +
        '<span class="kind-preview">' + esc(String(text.length)) + " chars</span>";
    det.appendChild(summary);
    const pre = document.createElement("pre");
    pre.textContent = text;
    det.appendChild(pre);
    return det;
}

function renderImage(b) {
    const pre = document.createElement("pre");
    pre.className = "image-block";
    pre.textContent = "[image]";
    return pre;
}

function renderUserText(b) {
    const pre = document.createElement("pre");
    pre.className = "user-text";
    pre.textContent = b.text || "";
    return pre;
}

function renderGeneric(b) {
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(b.raw, null, 2);
    return pre;
}

async function renderMessagesCompact(body, catalogByHash, onAddToCatalog) {
    const root = document.createElement("div");
    root.className = "messages";

    const units = await annotateSystemPrompt(body && body.system, catalogByHash);
    if (units.length) {
        root.appendChild(renderSystemPromptPseudo(units, onAddToCatalog));
    }

    const messages = (body && Array.isArray(body.messages)) ? body.messages : [];
    for (let i = 0; i < messages.length; i++) {
        const m = messages[i];
        if (!m || typeof m !== "object") continue;
        const cls = classifyMessage(m);
        root.appendChild(renderMessageCard(cls, i));
    }
    return root;
}

function renderMessagesRaw(body) {
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(body, null, 2);
    return pre;
}

// ---------------------------------------------------------------------------
// Dump list (existing behavior)
// ---------------------------------------------------------------------------
async function loadList() {
    if (mode !== "dumps") return;
    try {
        const r = await fetch("/api/requests");
        const data = await r.json();
        allItems = data.items || [];
    } catch (e) {
        console.error("list failed", e);
        allItems = [];
    }
    renderList();
    totalEl.textContent = "Total: " + allItems.length;
}

function renderList() {
    const q = searchEl.value.trim().toLowerCase();
    const filtered = q
        ? allItems.filter(it =>
            (it.path || "").toLowerCase().includes(q) ||
            (it.session_id || "").toLowerCase().includes(q))
        : allItems;

    if (filtered.length === 0) {
        listEl.innerHTML = "";
        if (allItems.length === 0) {
            emptyEl.textContent = "No captured requests yet. Start Claude Code against the proxy to see traffic here.";
        } else {
            emptyEl.textContent = "No requests match the filter.";
        }
        listEl.appendChild(emptyEl);
        emptyEl.style.display = "block";
        return;
    }
    emptyEl.style.display = "none";

    const groups = new Map();
    for (const it of filtered) {
        const key = it.session_id || NO_SESSION;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(it);
    }
    const groupKeys = [...groups.entries()].sort((a, b) => {
        if (a[0] === NO_SESSION) return 1;
        if (b[0] === NO_SESSION) return -1;
        return (b[1][0].ts || "").localeCompare(a[1][0].ts || "");
    });

    const frag = document.createDocumentFragment();
    for (const [key, items] of groupKeys) {
        const isCollapsed = collapsedSessions.has(key);

        const header = document.createElement("div");
        header.className = "session-header";
        header.innerHTML = `
            <span class="toggle">▾</span>
            <span class="session-label" title="${esc(key)}">${esc(key)}</span>
            <span class="session-count">${items.length}</span>
        `;

        const children = document.createElement("div");
        children.className = "session-children" + (isCollapsed ? " collapsed" : "");

        header.addEventListener("click", (e) => {
            e.stopPropagation();
            const nowCollapsed = children.classList.toggle("collapsed");
            if (nowCollapsed) collapsedSessions.add(key);
            else collapsedSessions.delete(key);
        });

        for (const it of items) {
            const row = document.createElement("div");
            row.className = "row" + (it.filename === selectedFile ? " selected" : "");
            row.dataset.file = it.filename;
            row.innerHTML = `
                <span class="n">${esc(String(it.n ?? "?").padStart(5, "0"))}</span>
                <span class="method ${esc(it.method || "")}">${esc(it.method || "")}</span>
                <span class="path" title="${esc(it.path || "")}">${esc(it.path || "")}</span>
                <span class="meta">${esc(fmtTime(it.ts))} ${esc(fmtSize(it.body_bytes_len))}</span>
            `;
            row.addEventListener("click", () => loadDetail(it.filename));
            children.appendChild(row);
        }

        frag.appendChild(header);
        frag.appendChild(children);
    }
    listEl.innerHTML = "";
    listEl.appendChild(frag);
}

// ---------------------------------------------------------------------------
// Dump detail
// ---------------------------------------------------------------------------
async function loadDetail(filename) {
    selectedFile = filename;
    listEl.querySelectorAll(".row").forEach(r => {
        r.classList.toggle("selected", r.dataset.file === filename);
    });
    detailEl.innerHTML = '<div class="placeholder">Loading...</div>';
    try {
        // Refresh the catalog cache before rendering so system-prompt names
        // show up even if the user hasn't visited catalog mode yet (or edits
        // happened in another tab).
        await fetchCatalogIfStale();
        const r = await fetch("/api/requests/" + encodeURIComponent(filename));
        if (!r.ok) {
            detailEl.innerHTML = `<div class="placeholder">Error ${r.status}: ${esc(await r.text())}</div>`;
            return;
        }
        const data = await r.json();
        await renderDetail(data);
    } catch (e) {
        detailEl.innerHTML = `<div class="placeholder">Failed: ${esc(String(e))}</div>`;
    }
}

let _catalogLastFetchedAt = 0;
async function fetchCatalogIfStale(maxAgeMs = 30000) {
    if (Date.now() - _catalogLastFetchedAt < maxAgeMs) return;
    try {
        const r = await fetch("/api/catalog/entries");
        if (!r.ok) return;
        const data = await r.json();
        catalogEntries = data.entries || [];
        catalogByHash = new Map(catalogEntries.map(e => [e.hash, e]));
        _catalogLastFetchedAt = Date.now();
    } catch (e) {
        // Ignore — we'll just render without names this round.
    }
}

async function renderDetail(d) {
    const body = d.body;

    const headers = d.headers || {};
    const headerRows = Object.entries(headers)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([k, v]) => {
            const redacted = v === "<redacted>";
            return `<tr><td class="k">${esc(k)}</td><td class="v${redacted ? " redacted" : ""}">${esc(String(v))}</td></tr>`;
        }).join("");

    detailEl.innerHTML = `
        <h2>Meta</h2>
        <div class="meta-grid">
            <span class="k">timestamp</span><span class="v">${esc(d.ts || "")}</span>
            <span class="k">method</span><span class="v">${esc(d.method || "")}</span>
            <span class="k">path</span><span class="v">${esc(d.path || "")}</span>
            <span class="k">query</span><span class="v">${esc(d.query || "(empty)")}</span>
            <span class="k">upstream</span><span class="v">${esc(d.upstream || "")}</span>
            <span class="k">body size</span><span class="v">${esc(String(d.body_bytes_len ?? "?"))} bytes</span>
        </div>

        <h2>Headers</h2>
        <table class="headers">${headerRows || '<tr><td class="k">(no headers)</td><td class="v"></td></tr>'}</table>

        <h2>Body</h2>
        <div id="bodyArea"></div>
    `;

    const bodyArea = document.getElementById("bodyArea");
    bodyArea.innerHTML = "";
    if (body && typeof body === "object" && body._raw_b64) {
        const pre = document.createElement("pre");
        pre.textContent = `binary, ${d.body_bytes_len ?? "?"} bytes (base64 in source file)`;
        bodyArea.appendChild(pre);
    } else if (viewMode === "compact") {
        const onAddToCatalog = (u) => {
            const suggested = (u.text.split("\n", 1)[0] || "")
                .slice(0, 40).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "prompt";
            setMode("catalog");
            showCatalogNewForm(u.text, suggested);
        };
        const compact = await renderMessagesCompact(body, catalogByHash, onAddToCatalog);
        bodyArea.appendChild(compact);
    } else {
        bodyArea.appendChild(renderMessagesRaw(body));
    }
}

// ---------------------------------------------------------------------------
// Prompt catalog list + detail
// ---------------------------------------------------------------------------
async function loadCatalogList() {
    try {
        const r = await fetch("/api/catalog/entries");
        if (!r.ok) {
            console.error("catalog list failed", r.status);
            catalogEntries = [];
        } else {
            const data = await r.json();
            catalogEntries = data.entries || [];
        }
    } catch (e) {
        console.error("catalog list failed", e);
        catalogEntries = [];
    }
    catalogByHash = new Map(catalogEntries.map(e => [e.hash, e]));
    renderCatalogList();
}

function renderCatalogList() {
    if (catalogEntries.length === 0) {
        listEl.innerHTML = "";
        emptyEl.textContent = "No catalog entries yet. Click '+ New' to add one, or open a captured request and click 'Add to catalog' on an unmatched system block.";
        listEl.appendChild(emptyEl);
        emptyEl.style.display = "block";
        return;
    }
    emptyEl.style.display = "none";
    const sorted = [...catalogEntries].sort((a, b) =>
        (b.updated_at || "").localeCompare(a.updated_at || "")
    );
    const frag = document.createDocumentFragment();
    for (const e of sorted) {
        const row = document.createElement("div");
        row.className = "row cat-row" + (e.hash === selectedCatalogHash ? " selected" : "");
        row.dataset.hash = e.hash;
        const short = e.hash.slice(7, 15); // skip "sha256:"
        row.innerHTML = `
            <span class="n">${esc(short)}</span>
            <span class="method CAT">CAT</span>
            <span class="path" title="${esc(e.text.slice(0, 200))}">${esc(e.name)}</span>
            <span class="meta">${esc(String(e.text.length))}c · ${esc(fmtTime(e.updated_at))}</span>
        `;
        row.addEventListener("click", () => loadCatalogEntry(e.hash));
        frag.appendChild(row);
    }
    listEl.innerHTML = "";
    listEl.appendChild(frag);
}

function _upsertCatalogEntry(entry) {
    const existing = catalogByHash.get(entry.hash);
    if (existing && existing.name !== entry.name) {
        // Name change: remove the entry under the old hash key (same hash, new name).
        // Since hash is the key, the entry just gets replaced in the Map; the
        // unique-name check on the server side guarantees no collisions.
        catalogByHash.delete(entry.hash);
    }
    catalogByHash.set(entry.hash, entry);
    const idx = catalogEntries.findIndex(e => e.hash === entry.hash);
    if (idx >= 0) catalogEntries[idx] = entry;
    else catalogEntries.push(entry);
}

function _removeCatalogEntry(hash) {
    catalogByHash.delete(hash);
    catalogEntries = catalogEntries.filter(e => e.hash !== hash);
}

function loadCatalogEntry(hash) {
    selectedCatalogHash = hash;
    listEl.querySelectorAll(".cat-row").forEach(r => {
        r.classList.toggle("selected", r.dataset.hash === hash);
    });
    const entry = catalogByHash.get(hash);
    if (!entry) {
        detailEl.innerHTML = '<div class="placeholder">Entry not found in cache — click Refresh.</div>';
        return;
    }
    renderCatalogDetail(entry);
}

function renderCatalogDetail(entry) {
    detailEl.innerHTML = `
        <h2>Catalog Entry</h2>
        <div class="meta-grid">
            <span class="k">hash</span><span class="v mono">${esc(entry.hash)}</span>
            <span class="k">created</span><span class="v">${esc(entry.created_at)}</span>
            <span class="k">updated</span><span class="v">${esc(entry.updated_at)}</span>
        </div>
        <label>Name <input id="catName" type="text" maxlength="64" value="${esc(entry.name)}"></label>
        <label>Text <textarea id="catText" readonly title="Text is immutable. To change it, delete and re-add."></textarea></label>
        <div class="cat-actions">
            <button id="catSave" class="button">Save</button>
            <button id="catCancel" class="button">Cancel</button>
            <button id="catDelete" class="button">Delete</button>
        </div>
        <p class="cat-hint">Text is hashed to identify the entry; rename only.</p>
    `;
    document.getElementById("catText").value = entry.text;

    document.getElementById("catSave").addEventListener("click", async () => {
        const newName = document.getElementById("catName").value.trim();
        if (!newName || newName === entry.name) return;
        const btn = document.getElementById("catSave");
        btn.disabled = true;
        try {
            const r = await fetch("/api/catalog/entries/" + encodeURIComponent(entry.hash), {
                method: "PUT",
                headers: { "content-type": "application/json" },
                body: JSON.stringify({ name: newName, text: entry.text }),
            });
            if (!r.ok) {
                const err = await r.json().catch(() => ({ detail: r.statusText }));
                alert("Save failed: " + JSON.stringify(err.detail || err));
                return;
            }
            const updated = await r.json();
            _upsertCatalogEntry(updated);
            renderCatalogList();
            loadCatalogEntry(updated.hash);
        } finally {
            btn.disabled = false;
        }
    });

    document.getElementById("catCancel").addEventListener("click", () => {
        loadCatalogEntry(entry.hash);
    });

    document.getElementById("catDelete").addEventListener("click", async () => {
        if (!confirm(`Delete "${entry.name}"?`)) return;
        const r = await fetch("/api/catalog/entries/" + encodeURIComponent(entry.hash), { method: "DELETE" });
        if (!r.ok) {
            alert("Delete failed: " + r.status);
            return;
        }
        _removeCatalogEntry(entry.hash);
        selectedCatalogHash = null;
        renderCatalogList();
        detailEl.innerHTML = '<div class="placeholder">Select a prompt to view or edit it.</div>';
    });
}

function showCatalogNewForm(text, suggestedName) {
    selectedCatalogHash = null;
    listEl.querySelectorAll(".cat-row").forEach(r => r.classList.remove("selected"));
    detailEl.innerHTML = `
        <h2>New Catalog Entry</h2>
        <label>Name <input id="catName" type="text" maxlength="64" value="${esc(suggestedName)}"></label>
        <label>Text <textarea id="catText" readonly></textarea></label>
        <div class="cat-actions">
            <button id="catCreate" class="button">Create</button>
            <button id="catCancel" class="button">Cancel</button>
        </div>
        <p class="cat-hint">Pick a short, descriptive name. The text is hashed to identify the entry.</p>
    `;
    document.getElementById("catText").value = text;

    document.getElementById("catCreate").addEventListener("click", async () => {
        const name = document.getElementById("catName").value.trim();
        if (!name) return;
        const btn = document.getElementById("catCreate");
        btn.disabled = true;
        try {
            const r = await fetch("/api/catalog/entries", {
                method: "POST",
                headers: { "content-type": "application/json" },
                body: JSON.stringify({ name, text }),
            });
            if (r.status === 409) {
                const err = await r.json().catch(() => ({ detail: r.statusText }));
                alert("Already cataloged: " + JSON.stringify(err.detail));
                return;
            }
            if (!r.ok) {
                alert("Create failed: " + r.status);
                return;
            }
            const entry = await r.json();
            _upsertCatalogEntry(entry);
            renderCatalogList();
            loadCatalogEntry(entry.hash);
        } finally {
            btn.disabled = false;
        }
    });

    document.getElementById("catCancel").addEventListener("click", () => {
        if (catalogEntries.length > 0) {
            loadCatalogList(); // refresh and select first
        } else {
            detailEl.innerHTML = '<div class="placeholder">Select a prompt to view or edit it.</div>';
        }
    });
}

// ---------------------------------------------------------------------------
// Systems list + detail
// ---------------------------------------------------------------------------
async function loadSystems() {
    try {
        const r = await fetch("/api/sessions");
        const data = await r.json();
        allSystems = data.items || [];
    } catch (e) {
        console.error("systems list failed", e);
        allSystems = [];
    }
    renderSystemsList();
}

function renderSystemsList() {
    if (allSystems.length === 0) {
        listEl.innerHTML = "";
        emptyEl.textContent = "No system-prompt aggregates yet. Run `python -m claude_proxy extract-system` to generate them.";
        listEl.appendChild(emptyEl);
        emptyEl.style.display = "block";
        return;
    }
    emptyEl.style.display = "none";

    const frag = document.createDocumentFragment();
    for (const s of allSystems) {
        const row = document.createElement("div");
        row.className = "row sys-row" + (s.filename === selectedSession ? " selected" : "");
        row.dataset.session = s.filename;
        const sidShort = ((s.session_id || "(no session)").slice(0, 8)) || "(none)";
        row.innerHTML = `
            <span class="n">${esc(sidShort)}</span>
            <span class="method POST">SYS</span>
            <span class="path" title="${esc(s.session_id || "")}">${esc(
                (s.request_count || 0) + " dumps · " +
                (s.distinct_full_prompts || 0) + " prompts · " +
                (s.distinct_system_reminders || 0) + " reminders"
            )}</span>
            <span class="meta">${esc(fmtTime(s.last_ts))}</span>
        `;
        row.addEventListener("click", () => loadSession(s.filename));
        frag.appendChild(row);
    }
    listEl.innerHTML = "";
    listEl.appendChild(frag);
}

async function loadSession(filename) {
    selectedSession = filename;
    listEl.querySelectorAll(".sys-row").forEach(r => {
        r.classList.toggle("selected", r.dataset.session === filename);
    });
    detailEl.innerHTML = '<div class="placeholder">Loading session...</div>';
    try {
        const r = await fetch("/api/sessions/" + encodeURIComponent(filename));
        if (!r.ok) {
            detailEl.innerHTML = `<div class="placeholder">Error ${r.status}: ${esc(await r.text())}</div>`;
            return;
        }
        const data = await r.json();
        renderSession(data);
    } catch (e) {
        detailEl.innerHTML = `<div class="placeholder">Failed: ${esc(String(e))}</div>`;
    }
}

function renderSession(s) {
    const sum = s.summary || {};
    detailEl.innerHTML = `
        <h2>Session</h2>
        <div class="meta-grid">
            <span class="k">session id</span><span class="v">${esc(s.session_id || "(none)")}</span>
            <span class="k">requests</span><span class="v">${esc(String(s.request_count ?? 0))}</span>
            <span class="k">first</span><span class="v">${esc(s.first_ts || "")}</span>
            <span class="k">last</span><span class="v">${esc(s.last_ts || "")}</span>
            <span class="k">last request</span><span class="v mono">${esc(s.last_request_filename || "—")}</span>
        </div>

        <h2>Summary</h2>
        <div class="meta-grid">
            <span class="k">total messages</span><span class="v">${esc(String(sum.total_messages ?? 0))}</span>
            <span class="k">real user turns</span><span class="v">${esc(String(sum.real_user_turns ?? 0))}</span>
            <span class="k">system-only turns</span><span class="v">${esc(String(sum.system_only_turns ?? 0))}</span>
            <span class="k">assistant turns</span><span class="v">${esc(String(sum.assistant_turns ?? 0))}</span>
            <span class="k">tool_use messages</span><span class="v">${esc(String(sum.tool_use_messages ?? 0))}</span>
            <span class="k">tool_result messages</span><span class="v">${esc(String(sum.tool_result_messages ?? 0))}</span>
            <span class="k">top-level blocks</span><span class="v">${esc(String(sum.top_level_blocks ?? 0))}</span>
            <span class="k">in-message blocks</span><span class="v">${esc(String(sum.in_message_blocks ?? 0))}</span>
        </div>

        <h2>Top-level system prompts</h2>
        <div id="topLevelList" class="sys-list"></div>

        <h2>In-message system reminders</h2>
        <div id="reminderList" class="sys-list"></div>

        <h2>Commands</h2>
        <div id="commandList" class="sys-list"></div>

        <h2>Continuations</h2>
        <div id="continuationList" class="sys-list"></div>

        <h2>Environments</h2>
        <div id="envList" class="sys-list"></div>
    `;

    renderTopLevelList(document.getElementById("topLevelList"), s.top_level_system || []);
    renderVariantList(document.getElementById("reminderList"), s.in_message_systems && s.in_message_systems.system_reminders || []);
    renderVariantList(document.getElementById("commandList"), s.in_message_systems && s.in_message_systems.commands || []);
    renderVariantList(document.getElementById("continuationList"), s.in_message_systems && s.in_message_systems.continuations || []);
    renderVariantList(document.getElementById("envList"), s.in_message_systems && s.in_message_systems.environments || []);

    // Delegated click handler for source_filename links: open the dump in
    // dumps mode. Done at the end so it covers the entire session detail.
    detailEl.querySelectorAll("a.source-link").forEach(a => {
        a.addEventListener("click", (e) => {
            e.preventDefault();
            const file = a.dataset.file;
            if (!file) return;
            history.pushState({}, "", "/");
            setMode("dumps");
            selectedFile = file;
            loadList().then(() => loadDetail(file));
        });
    });
}

// Helper: build a clickable "open this dump" link. Used in summary lines.
function _sourceLink(filename) {
    if (!filename) return "—";
    return `<a class="source-link" data-file="${esc(filename)}" href="#">${esc(filename)}</a>`;
}

// Build a single block row used by both renderTopLevelList and renderVariantList.
// - `label` is the kind label shown in the summary ("billing" / "full" / kind)
// - `meta` is the right-aligned metadata string ("req-…json #5")
// - `text` is the raw text; undefined when the block is fully represented
//   by a catalog_match.
// - `catalog_match` is {name, hash, ratio} when present.
function _appendBlockRow(det, { label, meta, text, catalog_match }) {
    if (catalog_match) {
        const ratioPct = Math.round((catalog_match.ratio || 0) * 100);
        const link = document.createElement("span");
        link.className = "prompt-link";
        link.textContent = catalog_match.name;
        link.title = `sha256: ${catalog_match.hash} · ${ratioPct}% match`;
        link.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            navigateToPrompt(catalog_match.name);
        });
        det.appendChild(link);
        const ratio = document.createElement("span");
        ratio.className = "match-ratio";
        ratio.textContent = `${ratioPct}%`;
        det.appendChild(ratio);
        return;
    }
    // No catalog match — show the raw text inline (per the user's plan: the
    // text is in the systems JSON for unmatched blocks so the user can
    // still see what was sent).
    if (typeof text === "string" && text) {
        const pre = document.createElement("pre");
        pre.className = "sys-block-text";
        pre.textContent = text;
        det.appendChild(pre);
        const addBtn = document.createElement("button");
        addBtn.className = "button cat-add";
        addBtn.textContent = "Add to catalog";
        addBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            _onAddToCatalogText(text);
        });
        det.appendChild(addBtn);
    }
}

// Frontend side of the "Add to catalog" flow (mirrors the dumps-mode handler).
function _onAddToCatalogText(text) {
    const suggested = (text.split("\n", 1)[0] || "")
        .slice(0, 40).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "prompt";
    history.pushState({}, "", "/catalog");
    setMode("catalog");
    // Make sure the catalog map is fresh, then open the new-entry form.
    fetch("/api/catalog/entries")
        .then(r => r.json())
        .then(data => {
            catalogEntries = data.entries || [];
            catalogByHash = new Map(catalogEntries.map(e => [e.hash, e]));
        })
        .catch(() => {})
        .finally(() => showCatalogNewForm(text, suggested));
}

function renderTopLevelList(target, items) {
    target.innerHTML = "";
    if (!items.length) {
        target.innerHTML = '<div class="placeholder small">None.</div>';
        return;
    }
    for (const v of items) {
        const det = document.createElement("details");
        det.className = "sys-block kind-top-level";
        det.open = false;
        const summary = document.createElement("summary");
        summary.innerHTML =
            '<span class="kind-label">' + esc(v.kind || "block") + '</span>' +
            '<span class="kind-meta">' + esc(v.length || 0) + " chars · " +
            _sourceLink(v.source_filename) + '</span>';
        det.appendChild(summary);
        _appendBlockRow(det, {
            label: v.kind,
            meta: v.source_filename,
            text: v.text,
            catalog_match: v.catalog_match,
        });
        target.appendChild(det);
    }
}

function renderVariantList(target, items) {
    target.innerHTML = "";
    if (!items.length) {
        target.innerHTML = '<div class="placeholder small">None.</div>';
        return;
    }
    for (const v of items) {
        const det = document.createElement("details");
        det.className = "sys-block kind-variant";
        det.open = false;
        const summary = document.createElement("summary");
        summary.innerHTML =
            '<span class="kind-preview">' + esc(preview(v.text || "", 100) || "—") + '</span>' +
            '<span class="kind-meta">' + esc(v.length || 0) + " chars · " +
            _sourceLink(v.source_filename) + ' #' + esc(String(v.source_msg_index ?? "?")) + '</span>';
        det.appendChild(summary);
        _appendBlockRow(det, {
            label: "block",
            meta: `${v.source_filename} #${v.source_msg_index}`,
            text: v.text,
            catalog_match: v.catalog_match,
        });
        target.appendChild(det);
    }
}

// ---------------------------------------------------------------------------
// Mode + view-mode toggling
// ---------------------------------------------------------------------------
function setMode(newMode) {
    if (newMode === mode) return;
    mode = newMode;
    document.body.dataset.mode = mode;
    sysBtn.classList.toggle("active", mode === "systems");
    catBtn.classList.toggle("active", mode === "catalog");
    searchEl.disabled = (mode !== "dumps");
    searchEl.value = "";
    clearInterval(autoTimer);
    autoTimer = null;

    if (mode === "systems") {
        loadSystems();
        detailEl.innerHTML = '<div class="placeholder">Select a session to view extracted system prompts.</div>';
    } else if (mode === "catalog") {
        loadCatalogList();
        detailEl.innerHTML = '<div class="placeholder">Select a prompt to view or edit it.</div>';
    } else { // dumps
        if (autoEl.checked) autoTimer = setInterval(loadList, 5000);
        loadList();
        if (selectedFile) {
            loadDetail(selectedFile);
        } else {
            detailEl.innerHTML = '<div class="placeholder">Select a request to view details.</div>';
        }
    }
}

function setViewMode(newMode) {
    viewMode = newMode;
    modeCompact.classList.toggle("active", newMode === "compact");
    modeRaw.classList.toggle("active", newMode === "raw");
    modeCompact.setAttribute("aria-pressed", newMode === "compact");
    modeRaw.setAttribute("aria-pressed", newMode === "raw");
    if (selectedFile && mode === "dumps") {
        loadDetail(selectedFile);
    }
}

// ---------------------------------------------------------------------------
// SPA routing (URL <-> mode). Minimal: handles /prompts/<name> as a real
// page; /, /systems, /catalog are stable URLs the topbar buttons can push.
// ---------------------------------------------------------------------------

function _pathForMode(m, name) {
    if (m === "prompt") return name ? "/prompts/" + encodeURIComponent(name) : "/";
    if (m === "systems") return "/systems";
    if (m === "catalog") return "/catalog";
    return "/";
}

function parseRoute() {
    const path = location.pathname.replace(/\/+$/, "") || "/";
    const m = path.match(/^\/prompts\/([^/]+)$/);
    if (m) {
        try {
            return { mode: "prompt", name: decodeURIComponent(m[1]) };
        } catch (e) {
            return { mode: "dumps" };
        }
    }
    if (path === "/systems") return { mode: "systems" };
    if (path === "/catalog") return { mode: "catalog" };
    return { mode: "dumps" };
}

function applyRoute() {
    const r = parseRoute();
    if (r.mode === "prompt") {
        if (mode !== "prompt" || promptName !== r.name) {
            // Hand off to the prompt loader; it owns setMode("prompt").
            loadPromptView(r.name);
        }
        return;
    }
    // For non-prompt routes, just sync the topbar and underlying state.
    if (mode !== r.mode) {
        setMode(r.mode);
    } else {
        // Already in the right mode (e.g. after pushState from a topbar
        // click). Just make sure the body dataset is consistent.
        document.body.dataset.mode = mode;
        sysBtn.classList.toggle("active", mode === "systems");
        catBtn.classList.toggle("active", mode === "catalog");
    }
}

function navigateToPrompt(name) {
    if (!name) return;
    const url = "/prompts/" + encodeURIComponent(name);
    if (location.pathname === url) {
        // Same URL — re-render in case data has changed.
        loadPromptView(name);
        return;
    }
    history.pushState({}, "", url);
    applyRoute();
}

// ---------------------------------------------------------------------------
// Prompt view: /prompts/<name> shows the full text + metadata + usage.
// ---------------------------------------------------------------------------

async function loadPromptView(name) {
    // Enter prompt mode first so CSS / topbar reflects the new view.
    if (mode !== "prompt") {
        mode = "prompt";
        document.body.dataset.mode = mode;
        sysBtn.classList.remove("active");
        catBtn.classList.remove("active");
        searchEl.disabled = true;
        searchEl.value = "";
        clearInterval(autoTimer);
        autoTimer = null;
    }
    promptName = name;
    detailEl.innerHTML = '<div class="placeholder">Loading prompt...</div>';
    try {
        const r = await fetch("/api/prompts/" + encodeURIComponent(name));
        if (r.status === 404) {
            detailEl.innerHTML = `<div class="placeholder">No prompt named "${esc(name)}" in the catalog.</div>`;
            return;
        }
        if (!r.ok) {
            detailEl.innerHTML = `<div class="placeholder">Error ${r.status}: ${esc(await r.text())}</div>`;
            return;
        }
        renderPromptView(await r.json());
    } catch (e) {
        detailEl.innerHTML = `<div class="placeholder">Failed: ${esc(String(e))}</div>`;
    }
}

function renderPromptView(p) {
    const refs = p.referenced_in || [];
    const truncated = !!p.referenced_in_truncated;
    const refRows = refs.map(r => `
        <tr class="row" data-file="${esc(r.filename)}">
            <td class="n">${esc((r.ts || "").slice(11, 19))}</td>
            <td class="mono">${esc(r.filename)}</td>
            <td>${esc(r.session_id ? r.session_id.slice(0, 12) : "—")}</td>
        </tr>
    `).join("");

    detailEl.innerHTML = `
        <h2>Prompt</h2>
        <div class="meta-grid">
            <span class="k">name</span><span class="v">${esc(p.name)}</span>
            <span class="k">hash</span><span class="v mono">${esc(p.hash)}</span>
            <span class="k">created</span><span class="v">${esc(p.created_at)}</span>
            <span class="k">updated</span><span class="v">${esc(p.updated_at)}</span>
            <span class="k">chars</span><span class="v">${esc(String((p.text || "").length))}</span>
        </div>
        <div class="cat-actions">
            <button id="promptOpenInCat" class="button">Open in catalog</button>
        </div>

        <h2>Text</h2>
        <textarea id="promptText" readonly title="Catalog text — editable only via the catalog view."></textarea>

        <h2>Referenced in (${refs.length}${truncated ? "+" : ""})</h2>
        ${refs.length === 0
            ? '<div class="placeholder small">No captured requests contain this prompt.</div>'
            : `<table class="ref-table">
                <thead><tr><th>time</th><th>file</th><th>session</th></tr></thead>
                <tbody>${refRows}</tbody>
               </table>`}
    `;
    document.getElementById("promptText").value = p.text || "";
    document.getElementById("promptOpenInCat").addEventListener("click", async () => {
        // Make sure the catalog map has this entry, then switch modes.
        try {
            const r = await fetch("/api/catalog/entries");
            const data = await r.json();
            catalogEntries = data.entries || [];
            catalogByHash = new Map(catalogEntries.map(e => [e.hash, e]));
        } catch (e) {
            console.error("catalog refresh failed", e);
        }
        const target = catalogByHash.get(p.hash);
        if (!target) {
            alert("Entry not found in catalog cache.");
            return;
        }
        history.pushState({}, "", "/catalog");
        setMode("catalog");
        loadCatalogList();
        loadCatalogEntry(p.hash);
    });

    // Make referenced-in rows clickable: switch to dumps mode and load.
    detailEl.querySelectorAll(".ref-table tr[data-file]").forEach(row => {
        row.addEventListener("click", () => {
            const file = row.dataset.file;
            if (!file) return;
            history.pushState({}, "", "/");
            setMode("dumps");
            selectedFile = file;
            loadList().then(() => loadDetail(file));
        });
    });
}

// ---------------------------------------------------------------------------
// Event wiring
// ---------------------------------------------------------------------------
refreshBtn.addEventListener("click", () => {
    if (mode === "dumps") loadList();
    else if (mode === "systems") loadSystems();
    else if (mode === "prompt") loadPromptView(promptName);
    else loadCatalogList();
});
searchEl.addEventListener("input", renderList);
autoEl.addEventListener("change", () => {
    if (autoEl.checked && mode === "dumps") {
        autoTimer = setInterval(loadList, 5000);
    } else {
        clearInterval(autoTimer);
        autoTimer = null;
    }
});
modeCompact.addEventListener("click", () => setViewMode("compact"));
modeRaw.addEventListener("click", () => setViewMode("raw"));
sysBtn.addEventListener("click", () => {
    if (mode === "systems") {
        history.pushState({}, "", "/");
        setMode("dumps");
    } else {
        history.pushState({}, "", "/systems");
        setMode("systems");
    }
});
catBtn.addEventListener("click", () => {
    if (mode === "catalog") {
        history.pushState({}, "", "/");
        setMode("dumps");
    } else {
        history.pushState({}, "", "/catalog");
        setMode("catalog");
    }
});

window.addEventListener("popstate", applyRoute);

applyRoute();
loadList();
