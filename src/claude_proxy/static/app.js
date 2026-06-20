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

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let mode = "dumps";            // "dumps" | "systems"
let viewMode = "compact";      // "compact" | "raw"
let allItems = [];
let allSystems = [];
let selectedFile = null;
let selectedSession = null;
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

function splitSystemPrompt(systemField) {
    const out = { billing: "", tagline: "", full: "", blocks: [] };
    if (typeof systemField === "string") {
        out.blocks.push(systemField);
        out.full = systemField;
        return out;
    }
    if (!Array.isArray(systemField)) return out;
    for (const b of systemField) {
        if (!b || b.type !== "text") continue;
        const text = b.text || "";
        out.blocks.push(text);
        const stripped = text.replace(/^\s+/, "");
        if (stripped.startsWith("x-anthropic-billing-header")) {
            out.billing = text;
        } else if (stripped.startsWith("You are Claude Code")) {
            out.tagline = text;
        } else {
            out.full = text;
        }
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

function renderSystemPromptPseudo(sys) {
    const det = document.createElement("details");
    det.className = "msg msg-system sys-prompt";
    det.open = false;
    const parts = [];
    if (sys.billing) parts.push("billing " + sys.billing.length + " chars");
    if (sys.tagline) parts.push("tagline " + sys.tagline.length + " chars");
    if (sys.full) parts.push("full " + sys.full.length + " chars");
    const summary = document.createElement("summary");
    summary.className = "msg-summary";
    summary.innerHTML =
        '<span class="role-badge role-system">system</span>' +
        '<span class="msg-idx">prompt</span>' +
        '<span class="msg-stats">' + esc(parts.join(" · ") || "(empty)") + '</span>';
    det.appendChild(summary);
    for (const block of sys.blocks) {
        const pre = document.createElement("pre");
        pre.textContent = block;
        det.appendChild(pre);
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

function renderMessagesCompact(body) {
    const root = document.createElement("div");
    root.className = "messages";

    const sys = splitSystemPrompt(body && body.system);
    if (sys.billing || sys.tagline || sys.full) {
        root.appendChild(renderSystemPromptPseudo(sys));
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
        const r = await fetch("/api/requests/" + encodeURIComponent(filename));
        if (!r.ok) {
            detailEl.innerHTML = `<div class="placeholder">Error ${r.status}: ${esc(await r.text())}</div>`;
            return;
        }
        const data = await r.json();
        renderDetail(data);
    } catch (e) {
        detailEl.innerHTML = `<div class="placeholder">Failed: ${esc(String(e))}</div>`;
    }
}

function renderDetail(d) {
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
        bodyArea.appendChild(renderMessagesCompact(body));
    } else {
        bodyArea.appendChild(renderMessagesRaw(body));
    }
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
        </div>

        <h2>Summary</h2>
        <div class="meta-grid">
            <span class="k">total messages</span><span class="v">${esc(String(sum.total_messages ?? 0))}</span>
            <span class="k">real user turns</span><span class="v">${esc(String(sum.real_user_turns ?? 0))}</span>
            <span class="k">system-only turns</span><span class="v">${esc(String(sum.system_only_turns ?? 0))}</span>
            <span class="k">assistant turns</span><span class="v">${esc(String(sum.assistant_turns ?? 0))}</span>
            <span class="k">tool_use messages</span><span class="v">${esc(String(sum.tool_use_messages ?? 0))}</span>
            <span class="k">tool_result messages</span><span class="v">${esc(String(sum.tool_result_messages ?? 0))}</span>
            <span class="k">distinct full prompts</span><span class="v">${esc(String(sum.distinct_full_prompts ?? 0))}</span>
            <span class="k">distinct billing headers</span><span class="v">${esc(String(sum.distinct_billing_headers ?? 0))}</span>
            <span class="k">distinct reminders</span><span class="v">${esc(String(sum.distinct_system_reminders ?? 0))}</span>
            <span class="k">distinct commands</span><span class="v">${esc(String(sum.distinct_commands ?? 0))}</span>
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

        <h2>Filenames (${(s.filenames || []).length})</h2>
        <pre>${esc((s.filenames || []).join("\n"))}</pre>
    `;

    renderTopLevelList(document.getElementById("topLevelList"), s.top_level_system || []);
    renderVariantList(document.getElementById("reminderList"), s.in_message_systems && s.in_message_systems.system_reminders || []);
    renderVariantList(document.getElementById("commandList"), s.in_message_systems && s.in_message_systems.commands || []);
    renderVariantList(document.getElementById("continuationList"), s.in_message_systems && s.in_message_systems.continuations || []);
    renderVariantList(document.getElementById("envList"), s.in_message_systems && s.in_message_systems.environments || []);
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
        const extras = v.distinct_count ? " · " + v.distinct_count + " distinct hashes" : "";
        summary.innerHTML =
            '<span class="kind-label">' + esc(v.kind) + '</span>' +
            '<span class="kind-preview">' + esc(v.length) + " chars · first=" +
            esc(v.first_filename) + esc(extras) + '</span>';
        det.appendChild(summary);
        const pre = document.createElement("pre");
        const txt = v.preview || "";
        pre.textContent = txt.length >= 200
            ? txt + "\n\n[... preview truncated; full text is in the source dump file]"
            : txt;
        det.appendChild(pre);
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
            '<span class="kind-count">×' + esc(String(v.count)) + '</span>' +
            '<span class="kind-preview">' + esc(preview(v.preview, 100)) + '</span>' +
            '<span class="kind-meta">' + esc(v.first_filename) + ' #' + esc(String(v.first_msg_index)) + '</span>';
        det.appendChild(summary);
        const samples = v.samples || [];
        for (const s of samples) {
            const pre = document.createElement("pre");
            pre.textContent = s.length > 800 ? s.slice(0, 800) + "\n\n[... truncated]" : s;
            det.appendChild(pre);
        }
        target.appendChild(det);
    }
}

// ---------------------------------------------------------------------------
// Mode + view-mode toggling
// ---------------------------------------------------------------------------
function setMode(newMode) {
    if (newMode === mode) return;
    mode = newMode;
    if (mode === "systems") {
        sysBtn.classList.add("active");
        clearInterval(autoTimer);
        autoTimer = null;
        loadSystems();
        detailEl.innerHTML = '<div class="placeholder">Select a session to view extracted system prompts.</div>';
    } else {
        sysBtn.classList.remove("active");
        if (autoEl.checked) {
            autoTimer = setInterval(loadList, 5000);
        }
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
// Event wiring
// ---------------------------------------------------------------------------
refreshBtn.addEventListener("click", () => {
    if (mode === "dumps") loadList();
    else loadSystems();
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
sysBtn.addEventListener("click", () => setMode(mode === "systems" ? "dumps" : "systems"));

loadList();
