const state = { repositories: [], authors: [], objects: [], selectedRepository: null, poller: null };
const $ = (selector) => document.querySelector(selector);
const number = new Intl.NumberFormat();
const compact = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
const colors = ["#145b3a", "#3878b8", "#c49327", "#9a5bb5", "#c94a3b", "#428f91"];

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const payload = response.status === 204 ? null : await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload?.error || `Request failed (${response.status})`);
  return payload;
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(node.timer);
  node.timer = setTimeout(() => node.classList.remove("show"), 3500);
}

function setBusy(busy, message = "Working") {
  $("#system-status").textContent = busy ? message : "Ready";
  document.body.style.cursor = busy ? "progress" : "";
}

async function loadRepositories(preferredId) {
  state.repositories = await api("/api/repositories");
  const select = $("#repository-select");
  const previous = preferredId || state.selectedRepository || Number(select.value);
  select.innerHTML = state.repositories.map(repository => `<option value="${repository.id}">${escapeHtml(repository.name)}</option>`).join("");
  $("#empty-state").classList.toggle("hidden", state.repositories.length > 0);
  $("#workspace").classList.toggle("hidden", state.repositories.length === 0);
  if (!state.repositories.length) return;
  state.selectedRepository = state.repositories.some(item => item.id === previous) ? previous : state.repositories[0].id;
  select.value = String(state.selectedRepository);
  await loadRepositoryData();
}

async function loadRepositoryData() {
  const repository = state.repositories.find(item => item.id === state.selectedRepository);
  if (!repository) return;
  const status = $("#repo-state");
  status.textContent = repository.status === "ready"
    ? `${number.format(repository.commit_count)} non-merge commits · ${repository.head_sha?.slice(0, 9) || "HEAD"}`
    : repository.status === "error" ? repository.error : `${repository.status}…`;
  status.classList.toggle("error", repository.status === "error");
  $("#scope-title").textContent = repository.name;
  const working = ["queued", "analyzing"].includes(repository.status);
  setBusy(working, repository.status === "queued" ? "Queued" : "Analyzing history");
  clearInterval(state.poller);
  if (working) {
    clearDashboard();
    state.poller = setInterval(async () => {
      const repositories = await api("/api/repositories");
      const updated = repositories.find(item => item.id === state.selectedRepository);
      state.repositories = repositories;
      if (updated && !["queued", "analyzing"].includes(updated.status)) {
        clearInterval(state.poller);
        await loadRepositories(updated.id);
      }
    }, 2500);
    return;
  }
  if (repository.status === "error") { clearDashboard(); return; }
  setBusy(false);
  [state.authors, state.objects] = await Promise.all([
    api(`/api/repositories/${repository.id}/authors`),
    api(`/api/repositories/${repository.id}/objects`),
  ]);
  renderFilterOptions();
  await loadAnalytics();
}

function renderFilterOptions() {
  const all = '<option value="">All authors</option>';
  $("#author-select").innerHTML = all + state.authors.map(author =>
    `<option value="${author.id}">${escapeHtml(author.name)} · ${escapeHtml(author.email)} (${author.commits})</option>`
  ).join("");
  const options = state.objects.map(item => `<option value="${escapeHtml(item.path)}">${item.kind}</option>`).join("");
  $("#object-options").innerHTML = options;
  const mergeOptions = state.authors.map(author =>
    `<option value="${author.id}">${escapeHtml(author.name)} &lt;${escapeHtml(author.email)}&gt;</option>`
  ).join("");
  $("#merge-source").innerHTML = mergeOptions;
  $("#merge-target").innerHTML = mergeOptions;
  if (state.authors.length > 1) $("#merge-target").selectedIndex = 1;
}

function filterQuery() {
  const params = new URLSearchParams();
  const author = $("#author-select").value;
  const path = $("#path-filter").value.trim();
  const kind = $("#kind-filter").value;
  const commits = $("#commit-filter").value.trim().split(/[\s,]+/).filter(Boolean).join(",");
  if (author) params.set("author_id", author);
  if (path) params.set("path", path);
  if (kind) params.set("kind", kind);
  if (commits) {
    params.set("commits", commits);
  } else {
    if ($("#date-start").value) params.set("start", String(Date.parse(`${$("#date-start").value}T00:00:00Z`) / 1000));
    if ($("#date-end").value) params.set("end", String(Date.parse(`${$("#date-end").value}T00:00:00Z`) / 1000));
  }
  return params;
}

async function loadAnalytics() {
  if (!state.selectedRepository) return;
  setBusy(true, "Calculating view");
  try {
    const result = await api(`/api/repositories/${state.selectedRepository}/analytics?${filterQuery()}`);
    renderDashboard(result);
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(false);
  }
}

function clearDashboard() {
  ["added", "removed", "growth", "churn"].forEach(key => $(`#metric-${key}`).textContent = "—");
  $("#commit-count").textContent = "0 commits";
  $("#metrics-body").innerHTML = "";
  $("#churn-chart").innerHTML = '<p class="empty-chart">Analysis results will appear here.</p>';
  $("#ownership-chart").innerHTML = '<p class="empty-chart">No ownership data.</p>';
}

function renderDashboard(result) {
  const focus = result.summary || result.objects[0] || { added: 0, removed: 0, growth: 0, churn: 0 };
  $("#metric-added").textContent = number.format(focus.added);
  $("#metric-removed").textContent = number.format(focus.removed);
  $("#metric-growth").textContent = `${focus.growth > 0 ? "+" : ""}${number.format(focus.growth)}`;
  $("#metric-growth").className = focus.growth < 0 ? "negative" : "positive";
  $("#metric-churn").textContent = number.format(focus.churn);
  $("#commit-count").textContent = `${number.format(result.commit_count)} commit${result.commit_count === 1 ? "" : "s"}`;
  const filterPath = $("#path-filter").value.trim();
  $("#scope-title").textContent = filterPath || result.repository.name;
  $("#ownership-focus").textContent = filterPath || "Repository root";
  renderBars(result.objects.filter(item => item.path !== "/").slice(0, 9));
  renderOwners(result.authors);
  renderTable(result.objects);
}

function renderBars(items) {
  const chart = $("#churn-chart");
  if (!items.length) { chart.innerHTML = '<p class="empty-chart">No changed objects in this commit set.</p>'; return; }
  const maximum = Math.max(...items.map(item => item.churn), 1);
  chart.innerHTML = items.map(item => `<div class="bar-row" title="${escapeHtml(item.path)}">
    <span class="bar-name">${escapeHtml(item.path)}</span><div class="bar-track"><div class="bar-fill" style="width:${item.churn / maximum * 100}%"></div></div><span class="bar-value">${compact.format(item.churn)}</span>
  </div>`).join("");
}

function renderOwners(authors) {
  const chart = $("#ownership-chart");
  if (!authors.length) { chart.innerHTML = '<p class="empty-chart">No churn is attributed in this scope.</p>'; return; }
  chart.innerHTML = authors.slice(0, 7).map((author, index) => {
    const initials = author.name.split(/\s+/).map(part => part[0]).join("").slice(0, 2).toUpperCase();
    return `<div class="owner-row"><span class="avatar" style="--owner:${colors[index % colors.length]}">${escapeHtml(initials)}</span><span class="owner-copy"><strong>${escapeHtml(author.name)}</strong><small>${number.format(author.churn)} churn · ${author.modifications} mods</small></span><span class="owner-percent">${(author.ownership * 100).toFixed(1)}%</span></div>`;
  }).join("");
}

function renderTable(items) {
  $("#metrics-body").innerHTML = items.map(item => `<tr data-path="${escapeHtml(item.path)}" data-kind="${item.kind}">
    <td class="path-cell" title="${escapeHtml(item.path)}">${escapeHtml(item.path)}</td><td><span class="kind-tag">${item.kind}</span></td>
    <td class="positive">+${number.format(item.added)}</td><td class="negative">−${number.format(item.removed)}</td><td class="${item.growth < 0 ? "negative" : "positive"}">${item.growth > 0 ? "+" : ""}${number.format(item.growth)}</td><td>${number.format(item.churn)}</td><td>${number.format(item.modifications)}</td><td>${(item.modification_frequency * 100).toFixed(1)}%</td><td>${item.churn_rate.toFixed(2)}</td>
  </tr>`).join("");
  $("#metrics-body").querySelectorAll("tr").forEach(row => row.addEventListener("click", () => {
    $("#path-filter").value = row.dataset.path;
    $("#kind-filter").value = row.dataset.kind;
    loadAnalytics();
    window.scrollTo({ top: 100, behavior: "smooth" });
  }));
}

function openRepositoryDialog() { $("#repository-dialog").showModal(); }
$("#add-repo-button").addEventListener("click", openRepositoryDialog);
document.querySelectorAll("[data-open-dialog]").forEach(button => button.addEventListener("click", openRepositoryDialog));
$("#refresh-button").addEventListener("click", () => loadRepositories(state.selectedRepository));
$("#repository-select").addEventListener("change", event => { state.selectedRepository = Number(event.target.value); loadRepositoryData(); });
$("#apply-filters").addEventListener("click", loadAnalytics);
$("#clear-filters").addEventListener("click", () => {
  ["#author-select", "#path-filter", "#kind-filter", "#date-start", "#date-end", "#commit-filter"].forEach(selector => $(selector).value = "");
  loadAnalytics();
});

document.querySelectorAll(".tab").forEach(tab => tab.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach(node => node.classList.toggle("active", node === tab));
  $("#clone-form").classList.toggle("hidden", tab.dataset.tab !== "clone");
  $("#upload-form").classList.toggle("hidden", tab.dataset.tab !== "upload");
}));

$("#clone-form").addEventListener("submit", async event => {
  event.preventDefault();
  const data = new FormData(event.target);
  try {
    const result = await api("/api/repositories/clone", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(Object.fromEntries(data)) });
    $("#repository-dialog").close(); event.target.reset(); toast("Clone queued. Analysis will begin automatically."); await loadRepositories(result.id);
  } catch (error) { toast(error.message); }
});

$("#upload-form").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    const result = await api("/api/repositories/upload", { method: "POST", body: new FormData(event.target) });
    $("#repository-dialog").close(); event.target.reset(); toast("Upload queued. Analysis will begin automatically."); await loadRepositories(result.id);
  } catch (error) { toast(error.message); }
});

$("#merge-open").addEventListener("click", () => {
  if (state.authors.length < 2) return toast("At least two author identities are required.");
  $("#merge-dialog").showModal();
});
$("#merge-form").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    await api(`/api/repositories/${state.selectedRepository}/authors/merge`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_id: Number($("#merge-source").value), target_id: Number($("#merge-target").value) }),
    });
    $("#merge-dialog").close(); toast("Author identities merged."); await loadRepositoryData();
  } catch (error) { toast(error.message); }
});

loadRepositories().catch(error => toast(error.message));
