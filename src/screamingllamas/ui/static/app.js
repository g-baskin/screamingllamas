"use strict";

// Crawled text is untrusted: everything is rendered with textContent, never innerHTML.

const PAGE_SIZE = 200;
const POLL_MS = 1000;
const LIVE_REFRESH_MS = 2000;

const view = {
  reports: null,
  tab: "internal",
  sort: null,
  desc: false,
  filter: "all",
  search: "",
  offset: 0,
  total: 0,
  hasCrawl: false,
  selected: null,
  detailTab: "details",
  info: null,
  lastState: null,
  lastStateText: "",
  lastLiveRefresh: 0,
  gridLoading: false,
  gridQueued: false,
};

const byId = (id) => document.getElementById(id);
const api = () => window.pywebview.api;

function make(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function fmt(n) {
  return typeof n === "number" ? n.toLocaleString() : "";
}

function describe(err) {
  return err && err.message ? err.message : String(err);
}

function showError(message) {
  const box = byId("error");
  box.textContent = message || "";
  box.hidden = !message;
}

// ---------- status ----------

function stateText(s) {
  switch (s.state) {
    case "idle":
      return "Ready. Enter a website address and press Start.";
    case "running":
      return `Crawling ${s.start_url}`;
    case "stopping":
      return "Stopping and saving…";
    case "done":
      if (s.result === "completed") return `Finished crawling ${s.start_url}`;
      if (s.result === "interrupted") return `Stopped. Everything crawled so far is saved.`;
      return `Crawl failed: ${s.error || "unknown error"}`;
    case "opened":
      return `Saved crawl of ${s.start_url}${s.result ? ` (${s.result})` : ""}`;
    default:
      return s.state;
  }
}

function countsText(s) {
  if (typeof s.crawled !== "number") return "";
  const parts = [`${fmt(s.crawled)} crawled`];
  if (typeof s.waiting === "number" && (s.state === "running" || s.state === "stopping")) {
    parts.push(`${fmt(s.waiting)} waiting`);
  }
  parts.push(`${fmt(s.blocked)} blocked by robots.txt`, `${fmt(s.errors)} without a response`);
  if (typeof s.elapsed === "number" && s.elapsed > 0) {
    parts.push(`${(s.crawled / s.elapsed).toFixed(1)} URLs/s`, `${fmt(Math.round(s.elapsed))}s`);
  }
  return parts.join(" · ");
}

function renderStatus(s) {
  const live = s.state === "running" || s.state === "stopping";
  byId("start").disabled = live;
  byId("stop").disabled = s.state !== "running";
  byId("open").disabled = live;
  const text = stateText(s);
  // Only touch the live region when the state changes, so screen readers aren't flooded.
  if (text !== view.lastStateText) {
    byId("state").textContent = text;
    view.lastStateText = text;
  }
  byId("counts").textContent = countsText(s);
  byId("folder").textContent = s.folder || "";
  byId("folder").title = s.folder || "";
  byId("show-folder").hidden = !s.folder;
}

async function refreshStatus() {
  const s = await api().status();
  const previous = view.lastState;
  view.lastState = s.state;
  renderStatus(s);
  const live = s.state === "running" || s.state === "stopping";
  if (live && Date.now() - view.lastLiveRefresh >= LIVE_REFRESH_MS) {
    view.lastLiveRefresh = Date.now();
    loadGrid();
  } else if (previous && previous !== s.state && !live) {
    loadGrid();
    if (view.selected) loadDetail(view.selected);
  }
}

async function poll() {
  try {
    await refreshStatus();
  } catch (err) {
    showError(describe(err));
  } finally {
    setTimeout(poll, POLL_MS);
  }
}

// ---------- crawl controls ----------

function lines(value) {
  return String(value || "")
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

async function onStart(event) {
  event.preventDefault();
  showError("");
  const data = new FormData(byId("crawl-form"));
  const options = {
    url: data.get("url") || "",
    max_urls: data.get("max_urls"),
    max_depth: data.get("max_depth"),
    concurrency: data.get("concurrency"),
    rate: data.get("rate"),
    timeout: data.get("timeout"),
    user_agent: data.get("user_agent") || "",
    ignore_robots: data.get("ignore_robots") === "on",
    follow_nofollow: data.get("follow_nofollow") === "on",
    include_subdomains: data.get("include_subdomains") === "on",
    check_external: data.get("check_external") === "on",
    include: lines(data.get("include")),
    exclude: lines(data.get("exclude")),
  };
  byId("start").disabled = true;
  try {
    const res = await api().start(options);
    if (!res.ok) {
      showError(res.error);
      byId("start").disabled = false;
      byId("url").focus();
      return;
    }
    resetResults();
    await refreshStatus();
  } catch (err) {
    showError(describe(err));
    byId("start").disabled = false;
  }
}

async function onStop() {
  try {
    const res = await api().stop();
    if (!res.ok) showError(res.error);
    await refreshStatus();
  } catch (err) {
    showError(describe(err));
  }
}

async function onOpen() {
  showError("");
  try {
    const res = await api().open_crawl();
    if (res.cancelled) return;
    if (!res.ok) {
      showError(res.error);
      return;
    }
    resetResults();
    await refreshStatus();
  } catch (err) {
    showError(describe(err));
  }
}

async function callAndReport(promise) {
  try {
    const res = await promise;
    if (!res.ok) showError(res.error);
  } catch (err) {
    showError(describe(err));
  }
}

function toggleSettings() {
  const panel = byId("settings");
  panel.hidden = !panel.hidden;
  byId("settings-toggle").setAttribute("aria-expanded", String(!panel.hidden));
}

// ---------- tabs (WAI-ARIA tabs pattern, automatic activation) ----------

function bindTabs(listId, onSelect) {
  const list = byId(listId);
  const tabs = Array.from(list.querySelectorAll('[role="tab"]'));
  const activate = (tab, focus) => {
    tabs.forEach((t) => {
      const on = t === tab;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
    });
    if (focus) tab.focus();
    onSelect(tab.dataset.tab, tab);
  };
  tabs.forEach((tab) => tab.addEventListener("click", () => activate(tab, false)));
  list.addEventListener("keydown", (event) => {
    const index = tabs.indexOf(document.activeElement);
    if (index < 0) return;
    const moves = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: tabs.length - 1 };
    if (!(event.key in moves)) return;
    event.preventDefault();
    activate(tabs[(moves[event.key] + tabs.length) % tabs.length], true);
  });
}

function setReport(tab, tabButton) {
  view.tab = tab;
  view.sort = null;
  view.desc = false;
  view.filter = "all";
  view.offset = 0;
  byId("grid-panel").setAttribute("aria-labelledby", tabButton.id);
  const select = byId("filter");
  select.replaceChildren(
    ...view.reports[tab].filters.map((f) => {
      const option = make("option", f.label);
      option.value = f.key;
      return option;
    }),
  );
  select.value = "all";
  loadGrid();
}

// ---------- results grid ----------

async function loadGrid() {
  if (view.gridLoading) {
    view.gridQueued = true;
    return;
  }
  view.gridLoading = true;
  try {
    const res = await api().rows(view.tab, {
      offset: view.offset,
      limit: PAGE_SIZE,
      sort: view.sort,
      desc: view.desc,
      filter: view.filter,
      search: view.search,
    });
    renderGrid(res);
  } catch (err) {
    showError(describe(err));
  } finally {
    view.gridLoading = false;
    if (view.gridQueued) {
      view.gridQueued = false;
      loadGrid();
    }
  }
}

function headerRow(columns, sortable) {
  const row = make("tr");
  columns.forEach((col) => {
    const th = make("th");
    th.scope = "col";
    if (col.numeric) th.classList.add("num");
    if (sortable) {
      th.setAttribute(
        "aria-sort",
        view.sort === col.key ? (view.desc ? "descending" : "ascending") : "none",
      );
      const button = make("button", col.label, "sort");
      button.type = "button";
      button.addEventListener("click", () => sortBy(col));
      th.append(button);
    } else {
      th.append(make("span", col.label));
    }
    row.append(th);
  });
  return row;
}

function renderGrid(res) {
  view.total = res.total;
  view.hasCrawl = res.has_crawl;
  const body = byId("grid").tBodies[0];
  const focused = body.contains(document.activeElement) ? document.activeElement.dataset.url : null;

  byId("grid").tHead.replaceChildren(headerRow(res.columns, true));
  const fragment = document.createDocumentFragment();
  res.rows.forEach((values) => {
    const tr = make("tr");
    tr.dataset.url = values[0];
    tr.tabIndex = -1;
    if (values[0] === view.selected) tr.setAttribute("aria-current", "true");
    values.forEach((value, i) => {
      const td = make("td", value);
      if (res.columns[i].numeric) td.classList.add("num");
      if (value !== null && value !== "") td.title = String(value);
      tr.append(td);
    });
    fragment.append(tr);
  });
  body.replaceChildren(fragment);

  const rows = Array.from(body.rows);
  const current = rows.find((r) => r.dataset.url === (focused || view.selected)) || rows[0];
  if (current) current.tabIndex = 0;
  if (focused && current && current.dataset.url === focused) current.focus({ preventScroll: true });

  const end = view.offset + res.rows.length;
  byId("range").textContent = res.total ? `${fmt(view.offset + 1)}–${fmt(end)} of ${fmt(res.total)}` : "";
  byId("prev").disabled = view.offset === 0;
  byId("next").disabled = end >= res.total;
  const empty = byId("grid-empty");
  empty.hidden = res.rows.length > 0;
  empty.textContent = res.has_crawl
    ? "No URLs match."
    : "Start a crawl, or open a saved one, to see URLs here.";
}

function sortBy(col) {
  if (view.sort === col.key) {
    view.desc = !view.desc;
  } else {
    view.sort = col.key;
    view.desc = col.numeric;
  }
  view.offset = 0;
  loadGrid();
}

function selectRow(tr, focus) {
  const body = byId("grid").tBodies[0];
  Array.from(body.rows).forEach((r) => {
    r.tabIndex = r === tr ? 0 : -1;
    if (r === tr) r.setAttribute("aria-current", "true");
    else r.removeAttribute("aria-current");
  });
  if (focus) tr.focus();
  if (view.selected !== tr.dataset.url) {
    view.selected = tr.dataset.url;
    loadDetail(view.selected);
  }
}

function onGridKey(event) {
  const tr = event.target.closest("tr");
  if (!tr) return;
  const rows = Array.from(byId("grid").tBodies[0].rows);
  const index = rows.indexOf(tr);
  const target = {
    ArrowDown: index + 1,
    ArrowUp: index - 1,
    Home: 0,
    End: rows.length - 1,
    PageDown: index + 20,
    PageUp: index - 20,
  }[event.key];
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    selectRow(tr, false);
  } else if (target !== undefined) {
    event.preventDefault();
    selectRow(rows[Math.max(0, Math.min(rows.length - 1, target))], true);
  }
}

function resetResults() {
  view.offset = 0;
  view.selected = null;
  view.info = null;
  renderDetail();
  loadGrid();
}

// ---------- detail pane ----------

async function loadDetail(url) {
  try {
    const info = await api().url_info(url);
    if (view.selected !== url) return;
    view.info = info;
    renderDetail();
  } catch (err) {
    showError(describe(err));
  }
}

function renderDetail() {
  const info = view.info;
  byId("detail-url").textContent = view.selected || "";
  byId("detail-url").title = view.selected || "";
  byId("open-url").disabled = !view.selected;
  const counts = { inlinks: info ? info.inlinks.total : null, outlinks: info ? info.outlinks.total : null };
  byId("detail-tabs").querySelectorAll('[role="tab"]').forEach((tab) => {
    const base = tab.dataset.tab.charAt(0).toUpperCase() + tab.dataset.tab.slice(1);
    const n = counts[tab.dataset.tab];
    tab.textContent = typeof n === "number" ? `${base} (${fmt(n)})` : base;
  });

  const table = byId("detail");
  let columns = [];
  let rows = [];
  let note = "";
  let emptyText = "Select a URL above to see its details.";
  if (info) {
    if (view.detailTab === "details" || view.detailTab === "headers") {
      columns = view.detailTab === "details" ? ["Field", "Value"] : ["Header", "Value"];
      rows = view.detailTab === "details" ? info.details : info.headers;
      emptyText = info.crawled ? "Nothing to show." : "This URL was found but not requested.";
    } else {
      const links = info[view.detailTab];
      columns = links.columns;
      rows = links.rows;
      emptyText = "No links.";
      if (links.total > rows.length) note = `Showing the first ${fmt(rows.length)} of ${fmt(links.total)}`;
    }
  }
  table.classList.toggle("kv", view.detailTab === "details" || view.detailTab === "headers");
  table.tHead.replaceChildren(
    ...(columns.length ? [headerRow(columns.map((label) => ({ label, numeric: label === "Status Code" })), false)] : []),
  );
  const fragment = document.createDocumentFragment();
  rows.forEach((values) => {
    const tr = make("tr");
    values.forEach((value, i) => {
      const td = make("td", value);
      if (columns[i] === "Status Code") td.classList.add("num");
      if (value !== null && value !== "") td.title = String(value);
      tr.append(td);
    });
    fragment.append(tr);
  });
  table.tBodies[0].replaceChildren(fragment);
  byId("detail-note").textContent = note;
  const empty = byId("detail-empty");
  empty.hidden = rows.length > 0;
  empty.textContent = emptyText;
}

// ---------- wiring ----------

let searchTimer = 0;

function bindEvents() {
  byId("crawl-form").addEventListener("submit", onStart);
  byId("stop").addEventListener("click", onStop);
  byId("open").addEventListener("click", onOpen);
  byId("settings-toggle").addEventListener("click", toggleSettings);
  byId("show-folder").addEventListener("click", () => callAndReport(api().show_folder()));
  byId("open-url").addEventListener("click", () => {
    if (view.selected) callAndReport(api().open_in_browser(view.selected));
  });
  byId("filter").addEventListener("change", (event) => {
    view.filter = event.target.value;
    view.offset = 0;
    loadGrid();
  });
  byId("search").addEventListener("input", (event) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      view.search = event.target.value;
      view.offset = 0;
      loadGrid();
    }, 250);
  });
  byId("prev").addEventListener("click", () => {
    view.offset = Math.max(0, view.offset - PAGE_SIZE);
    loadGrid();
  });
  byId("next").addEventListener("click", () => {
    view.offset += PAGE_SIZE;
    loadGrid();
  });
  const body = byId("grid").tBodies[0];
  body.addEventListener("click", (event) => {
    const tr = event.target.closest("tr");
    if (tr) selectRow(tr, false);
  });
  body.addEventListener("keydown", onGridKey);
  bindTabs("report-tabs", setReport);
  bindTabs("detail-tabs", (tab) => {
    view.detailTab = tab;
    renderDetail();
  });
}

let started = false;

async function init() {
  if (started) return;
  started = true;
  try {
    view.reports = await api().reports();
  } catch (err) {
    showError(describe(err));
    return;
  }
  bindEvents();
  setReport("internal", byId("tab-internal"));
  renderDetail();
  poll();
  byId("url").focus();
}

window.addEventListener("pywebviewready", init);
if (window.pywebview && window.pywebview.api && window.pywebview.api.reports) init();
