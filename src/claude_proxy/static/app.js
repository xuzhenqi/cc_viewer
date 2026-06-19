const listEl = document.getElementById("list");
const emptyEl = document.getElementById("empty");
const detailEl = document.getElementById("detail");
const totalEl = document.getElementById("total");
const searchEl = document.getElementById("search");
const refreshBtn = document.getElementById("refresh");
const autoEl = document.getElementById("auto");

let allItems = [];
let selectedFile = null;
let autoTimer = null;

function fmtTime(ts) {
    if (!ts) return "";
    const d = new Date(ts);
    if (isNaN(d)) return ts;
    return d.toLocaleTimeString([], {hour12: false});
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

async function loadList() {
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
        ? allItems.filter(it => (it.path || "").toLowerCase().includes(q))
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

    const frag = document.createDocumentFragment();
    for (const it of filtered) {
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
        frag.appendChild(row);
    }
    listEl.innerHTML = "";
    listEl.appendChild(frag);
}

async function loadDetail(filename) {
    selectedFile = filename;
    document.querySelectorAll(".row").forEach(r => {
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
    let bodyHtml;
    if (body && typeof body === "object" && body._raw_b64) {
        bodyHtml = `<pre>binary, ${esc(String(d.body_bytes_len ?? "?"))} bytes (base64 in source file)</pre>`;
    } else {
        bodyHtml = `<pre>${esc(JSON.stringify(body, null, 2))}</pre>`;
    }

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
        ${bodyHtml}
    `;
}

refreshBtn.addEventListener("click", loadList);
searchEl.addEventListener("input", renderList);
autoEl.addEventListener("change", () => {
    if (autoEl.checked) {
        autoTimer = setInterval(loadList, 5000);
    } else {
        clearInterval(autoTimer);
        autoTimer = null;
    }
});

loadList();
