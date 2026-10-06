const state = { repositories: [], authors: [], objects: [], selectedRepository: null, poller: null, bucket: "week" };
const $ = (selector) => document.querySelector(selector);
const number = new Intl.NumberFormat();
const compact = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
const colors = ["#145b3a", "#3878b8", "#c49327", "#9a5bb5", "#c94a3b", "#428f91"];

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);
}

const svgNS = "http://www.w3.org/2000/svg";

function svgRoot(container, width, height, label) {
  container.innerHTML = "";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", label);
  svg.classList.add("chart-svg");
  container.appendChild(svg);
  return svg;
}

function svgNode(parent, name, attributes = {}, text) {
  const node = document.createElementNS(svgNS, name);
  Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
  if (text !== undefined) node.textContent = text;
  parent.appendChild(node);
  return node;
}

function shortPath(path, length = 26) {
  if (path.length <= length) return path;
  const parts = path.split("/");
  let tail = parts.pop();
  while (parts.length && (parts[parts.length - 1] + "/" + tail).length + 1 <= length) tail = parts.pop() + "/" + tail;
  return `…/${tail.length > length ? tail.slice(-length) : tail}`;
}

function median(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const middle = sorted.length >> 1;
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

function focusObject(path, kind) {
  $("#path-filter").value = path;
  $("#kind-filter").value = kind;
  loadAnalytics();
  window.scrollTo({ top: 100, behavior: "smooth" });
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
    const query = filterQuery();
    const repository = state.selectedRepository;
    const [analytics, insights, timeline, coupling] = await Promise.all([
      api(`/api/repositories/${repository}/analytics?${query}`),
      api(`/api/repositories/${repository}/insights?${query}`),
      api(`/api/repositories/${repository}/timeline?${query}&bucket=${state.bucket}`),
      api(`/api/repositories/${repository}/coupling?${query}`),
    ]);
    renderDashboard(analytics);
    renderInsights(insights);
    renderTimeline(timeline);
    renderCoupling(coupling);
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(false);
  }
}

async function fetchTimeline() {
  if (!state.selectedRepository) return;
  try {
    const data = await api(`/api/repositories/${state.selectedRepository}/timeline?${filterQuery()}&bucket=${state.bucket}`);
    renderTimeline(data);
  } catch (error) {
    toast(error.message);
  }
}

function clearDashboard() {
  ["added", "removed", "growth", "churn"].forEach(key => $(`#metric-${key}`).textContent = "—");
  ["#chip-bus-factor", "#chip-gini", "#chip-owners80", "#chip-files80", "#chip-trend"].forEach(selector => $(selector).textContent = "—");
  $("#commit-count").textContent = "0 commits";
  $("#metrics-body").innerHTML = "";
  $("#churn-chart").innerHTML = '<p class="empty-chart">Analysis results will appear here.</p>';
  $("#ownership-chart").innerHTML = '<p class="empty-chart">No ownership data.</p>';
  $("#pareto-chart").innerHTML = "";
  $("#timeline-chart").innerHTML = '<p class="empty-chart">The timeline will appear after analysis.</p>';
  $("#scatter-chart").innerHTML = '<p class="empty-chart">Hotspots will appear after analysis.</p>';
  $("#coupling-chart").innerHTML = '<p class="empty-chart">Coupling will appear after analysis.</p>';
  $("#coupling-pairs").innerHTML = "";
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
  renderScatter(result.objects);
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

function renderInsights(insights) {
  const authors = insights.authors || {};
  const files = insights.files || {};
  const giniNode = $("#chip-gini");
  giniNode.textContent = (authors.gini || 0).toFixed(2);
  giniNode.closest(".chip").title = `HHI ${(authors.hhi || 0).toFixed(2)} · ${authors.items || 0} authors · top author carries ${((authors.top_share || 0) * 100).toFixed(0)}% of churn`;
  $("#chip-bus-factor").textContent = `${authors.bus_factor || 0} of ${authors.items || 0}`;
  $("#chip-owners80").textContent = `${authors.coverage_80?.items || 0} of ${authors.items || 0}`;
  const files80 = files.coverage_80;
  const filesNode = $("#chip-files80");
  filesNode.textContent = files80 && files80.total_items ? `${(files80.share * 100).toFixed(0)}%` : "—";
  filesNode.closest(".chip").title = files80 && files80.total_items
    ? `${files80.items} of ${files80.total_items} files carry 80% of churn`
    : "No file churn in this scope";
  renderPareto(insights.pareto_curve);
}

function renderPareto(curve) {
  const container = $("#pareto-chart");
  if (!curve || curve.length < 2) { container.innerHTML = ""; return; }
  const width = 300, height = 104;
  const pad = { top: 10, right: 12, bottom: 14, left: 36 };
  const innerWidth = width - pad.left - pad.right;
  const innerHeight = height - pad.top - pad.bottom;
  const px = value => pad.left + value * innerWidth;
  const py = value => pad.top + innerHeight - value * innerHeight;
  const svg = svgRoot(container, width, height, "Cumulative churn concentration curve");
  const guide = svgNode(svg, "line", { x1: pad.left, x2: width - pad.right, y1: py(0.8), y2: py(0.8), class: "pareto-guide" });
  svgNode(guide, "title", {}, "80% of churn");
  const area = `M ${px(0)},${py(0)} ` + curve.map(([sx, sy]) => `L ${px(sx)},${py(sy)}`).join(" ") + ` L ${px(1)},${py(0)} Z`;
  svgNode(svg, "path", { d: area, class: "pareto-area" });
  svgNode(svg, "polyline", { points: curve.map(([sx, sy]) => `${px(sx)},${py(sy)}`).join(" "), class: "pareto-line" });
  const crossing = curve.find(([, sy]) => sy >= 0.8);
  if (crossing) {
    const marker = svgNode(svg, "circle", { cx: px(crossing[0]), cy: py(crossing[1]), r: 4, class: "pareto-marker" });
    svgNode(marker, "title", {}, `${(crossing[0] * 100).toFixed(0)}% of files carry ${(crossing[1] * 100).toFixed(0)}% of churn`);
  }
  svgNode(svg, "text", { x: pad.left - 6, y: py(1) + 4, "text-anchor": "end", class: "axis-label" }, "100%");
  svgNode(svg, "text", { x: pad.left - 6, y: py(0) + 4, "text-anchor": "end", class: "axis-label" }, "0%");
  svgNode(svg, "text", { x: px(1), y: height - 2, "text-anchor": "end", class: "axis-label" }, "files ranked by churn →");
}

function renderTimeline(data) {
  const trend = data.trend || { slope: 0, r2: 0, n: 0 };
  const per = data.bucket === "month" ? "month" : "week";
  const trendChip = $("#chip-trend");
  trendChip.textContent = Math.abs(trend.slope) < 1 ? "stable" : `${trend.slope > 0 ? "↑" : "↓"} ${compact.format(Math.abs(trend.slope))} lines/${per}`;
  trendChip.closest(".chip").title = `Least-squares fit over ${trend.n || 0} buckets · R² ${Number(trend.r2 || 0).toFixed(2)}`;
  const container = $("#timeline-chart");
  const points = data.points || [];
  if (!points.length) { container.innerHTML = '<p class="empty-chart">No commits in this scope.</p>'; return; }
  // Coalesce dense histories into at most ~180 display slots so bars stay readable.
  const slotSize = Math.max(1, Math.ceil(points.length / 180));
  const bars = [];
  for (let index = 0; index < points.length; index += slotSize) {
    const slot = points.slice(index, index + slotSize);
    bars.push({
      label: slot[0].label,
      endLabel: slot[slot.length - 1].label,
      span: slot.length,
      commits: slot.reduce((sum, point) => sum + point.commits, 0),
      added: slot.reduce((sum, point) => sum + point.added, 0),
      removed: slot.reduce((sum, point) => sum + point.removed, 0),
      cumulative_net: slot[slot.length - 1].cumulative_net,
    });
  }
  const width = 900, height = 300;
  const pad = { top: 22, right: 84, bottom: 44, left: 64 };
  const innerWidth = width - pad.left - pad.right;
  const innerHeight = height - pad.top - pad.bottom;
  const half = innerHeight / 2;
  const zeroY = pad.top + half;
  const maxAdded = Math.max(...bars.map(bar => bar.added), 1);
  const maxRemoved = Math.max(...bars.map(bar => bar.removed), 1);
  const maxChurn = Math.max(...bars.map(bar => bar.added + bar.removed), 1);
  const maxCumulative = Math.max(...bars.map(bar => Math.abs(bar.cumulative_net)), 1);
  const step = innerWidth / bars.length;
  const svg = svgRoot(container, width, height, "Change timeline");
  const center = index => pad.left + index * step + step / 2;
  [0.5, 1].forEach(fraction => {
    svgNode(svg, "line", { x1: pad.left, x2: width - pad.right, y1: zeroY - half * fraction, y2: zeroY - half * fraction, class: "grid-line" });
    svgNode(svg, "line", { x1: pad.left, x2: width - pad.right, y1: zeroY + half * fraction, y2: zeroY + half * fraction, class: "grid-line" });
  });
  svgNode(svg, "line", { x1: pad.left, x2: width - pad.right, y1: zeroY, y2: zeroY, class: "zero-line" });
  svgNode(svg, "text", { x: pad.left - 8, y: zeroY - half + 4, "text-anchor": "end", class: "axis-label" }, compact.format(maxAdded));
  svgNode(svg, "text", { x: pad.left - 8, y: zeroY - half / 2 + 4, "text-anchor": "end", class: "axis-label" }, compact.format(maxAdded / 2));
  svgNode(svg, "text", { x: pad.left - 8, y: zeroY + 4, "text-anchor": "end", class: "axis-label" }, "0");
  svgNode(svg, "text", { x: pad.left - 8, y: zeroY + half / 2 + 4, "text-anchor": "end", class: "axis-label" }, compact.format(maxRemoved / 2));
  svgNode(svg, "text", { x: pad.left - 8, y: zeroY + half + 4, "text-anchor": "end", class: "axis-label" }, compact.format(maxRemoved));
  svgNode(svg, "text", { x: width - pad.right + 10, y: zeroY - half + 4, class: "axis-label" }, `Σ ${compact.format(maxCumulative)}`);
  svgNode(svg, "text", { x: width - pad.right + 10, y: zeroY + half + 4, class: "axis-label" }, `Σ −${compact.format(maxCumulative)}`);
  const barWidth = Math.max(2.5, Math.min(step * 0.62, 34));
  const labelStride = Math.max(1, Math.ceil(bars.length / 9));
  bars.forEach((point, index) => {
    const x = center(index);
    const group = svgNode(svg, "g", {});
    const addedHeight = (point.added / maxAdded) * half;
    if (addedHeight > 0.6) svgNode(group, "rect", { x: x - barWidth / 2, y: zeroY - addedHeight, width: barWidth, height: addedHeight, rx: 2, class: "bar-added" });
    const removedHeight = (point.removed / maxRemoved) * half;
    if (removedHeight > 0.6) svgNode(group, "rect", { x: x - barWidth / 2, y: zeroY, width: barWidth, height: removedHeight, rx: 2, class: "bar-removed" });
    const hover = svgNode(group, "rect", { x: pad.left + index * step, y: pad.top, width: step, height: innerHeight, fill: "transparent", "pointer-events": "all" });
    const range = point.span > 1 ? ` → ${point.endLabel}` : "";
    svgNode(hover, "title", {}, `${point.label}${range} · ${number.format(point.commits)} commits · +${number.format(point.added)} / −${number.format(point.removed)} · cumulative ${point.cumulative_net >= 0 ? "+" : "−"}${number.format(Math.abs(point.cumulative_net))}`);
    if (index % labelStride === 0) svgNode(svg, "text", { x, y: height - 16, "text-anchor": "middle", class: "axis-label" }, data.bucket === "month" ? point.label : point.label.slice(5));
  });
  const cumulativePoints = bars.map((point, index) => `${center(index)},${zeroY - (point.cumulative_net / maxCumulative) * half}`).join(" ");
  const cumulativeNode = svgNode(svg, "polyline", { points: cumulativePoints, class: "cumulative-line" });
  svgNode(cumulativeNode, "title", {}, "Cumulative net growth (right scale)");
  if (bars.length >= 2 && trend.slope) {
    const trendPoints = bars.map((_, index) => {
      const value = Math.min(maxChurn, Math.max(0, trend.intercept + trend.slope * index * slotSize));
      return `${center(index)},${zeroY - (value / maxChurn) * half}`;
    }).join(" ");
    const trendNode = svgNode(svg, "polyline", { points: trendPoints, class: "trend-line" });
    svgNode(trendNode, "title", {}, `Churn trend ${trend.slope > 0 ? "+" : "−"}${compact.format(Math.abs(trend.slope))} lines per ${per} · R² ${Number(trend.r2).toFixed(2)}`);
  }
}

function renderScatter(objects) {
  const container = $("#scatter-chart");
  const files = (objects || []).filter(item => item.kind === "file" && item.churn > 0).slice(0, 400);
  if (!files.length) { container.innerHTML = '<p class="empty-chart">No file-level changes in this scope.</p>'; return; }
  const width = 620, height = 340;
  const pad = { top: 26, right: 18, bottom: 50, left: 62 };
  const innerWidth = width - pad.left - pad.right;
  const innerHeight = height - pad.top - pad.bottom;
  const maxFrequency = Math.max(...files.map(file => file.modification_frequency), 0.02);
  const maxChurn = Math.max(...files.map(file => file.churn), 1);
  const px = value => pad.left + (value / maxFrequency) * innerWidth;
  const py = value => pad.top + innerHeight - (value / maxChurn) * innerHeight;
  const svg = svgRoot(container, width, height, "Churn versus modification frequency scatter plot");
  [0, 0.25, 0.5, 0.75, 1].forEach(fraction => {
    const y = py(maxChurn * fraction);
    svgNode(svg, "line", { x1: pad.left, x2: width - pad.right, y1: y, y2: y, class: fraction ? "grid-line" : "zero-line" });
    if (fraction) svgNode(svg, "text", { x: pad.left - 8, y: y + 4, "text-anchor": "end", class: "axis-label" }, compact.format(Math.round(maxChurn * fraction)));
  });
  [0.25, 0.5, 0.75, 1].forEach(fraction => {
    svgNode(svg, "line", { x1: px(maxFrequency * fraction), x2: px(maxFrequency * fraction), y1: pad.top, y2: pad.top + innerHeight, class: "grid-line" });
  });
  [0.5, 1].forEach(fraction => {
    svgNode(svg, "text", { x: px(maxFrequency * fraction), y: height - 28, "text-anchor": "middle", class: "axis-label" }, `${(maxFrequency * fraction * 100).toFixed(0)}%`);
  });
  svgNode(svg, "text", { x: pad.left + innerWidth / 2, y: height - 8, "text-anchor": "middle", class: "axis-label" }, "modification frequency (share of commits touching the file)");
  svgNode(svg, "text", { x: pad.left - 8, y: pad.top - 12, "text-anchor": "end", class: "axis-label" }, "churn");
  svgNode(svg, "text", { x: width - pad.right, y: pad.top + 12, "text-anchor": "end", class: "axis-label" }, "hot & volatile ↗");
  const medianFrequency = median(files.map(file => file.modification_frequency));
  const medianChurn = median(files.map(file => file.churn));
  svgNode(svg, "line", { x1: px(medianFrequency), x2: px(medianFrequency), y1: pad.top, y2: pad.top + innerHeight, class: "median-line" });
  svgNode(svg, "line", { x1: pad.left, x2: width - pad.right, y1: py(medianChurn), y2: py(medianChurn), class: "median-line" });
  files.forEach(file => {
    const dot = svgNode(svg, "circle", {
      cx: px(file.modification_frequency), cy: py(file.churn), r: 4.4,
      class: "scatter-dot", fill: file.growth < 0 ? "#c94a3b" : "#145b3a",
    });
    svgNode(dot, "title", {}, `${file.path}\nchurn ${number.format(file.churn)} · ${number.format(file.modifications)} modifications · ${(file.modification_frequency * 100).toFixed(1)}% frequency · ${file.churn_rate.toFixed(2)} churn/commit`);
    dot.addEventListener("click", () => focusObject(file.path, "file"));
  });
}

function renderCoupling(data) {
  const container = $("#coupling-chart");
  const pairsBox = $("#coupling-pairs");
  const files = data.files || [];
  const pairs = data.pairs || [];
  pairsBox.innerHTML = "";
  if (files.length < 2) { container.innerHTML = '<p class="empty-chart">Not enough overlapping files to compare.</p>'; return; }
  const pairIndex = new Map(pairs.map(pair => [`${pair.a}\u0000${pair.b}`, pair]));
  const maximum = Math.max(...pairs.map(pair => pair.together), 1);
  const cell = 30, labelWidth = 172, topHeight = 84;
  const width = labelWidth + files.length * cell + 14;
  const height = topHeight + files.length * cell + 24;
  const svg = svgRoot(container, width, height, "Co-change coupling matrix");
  files.forEach((file, index) => {
    const x = labelWidth + index * cell + cell / 2;
    svgNode(svg, "text", { x: labelWidth - 8, y: topHeight + index * cell + cell / 2 + 4, "text-anchor": "end", class: "axis-label heat-label" }, shortPath(file.path, 24));
    svgNode(svg, "text", { x, y: topHeight - 12, class: "axis-label heat-label", transform: `rotate(-50 ${x} ${topHeight - 12})` }, shortPath(file.path, 20));
  });
  files.forEach((rowFile, row) => {
    files.forEach((colFile, col) => {
      const key = rowFile.path < colFile.path ? `${rowFile.path}\u0000${colFile.path}` : `${colFile.path}\u0000${rowFile.path}`;
      const pair = row === col ? null : pairIndex.get(key);
      const x = labelWidth + col * cell, y = topHeight + row * cell;
      const rect = svgNode(svg, "rect", {
        x: x + 1.5, y: y + 1.5, width: cell - 3, height: cell - 3, rx: 5,
        class: pair ? "heat-cell clickable" : "heat-cell",
        fill: row === col ? "#e6e8e0" : pair ? "#145b3a" : "#f3f3ec",
        "fill-opacity": pair ? 0.2 + 0.8 * (pair.together / maximum) : 1,
      });
      if (pair) {
        svgNode(rect, "title", {}, `${rowFile.path} ↔ ${colFile.path}\n${pair.together} shared commits · lift ${pair.lift.toFixed(2)}× · confidence ${(pair.confidence * 100).toFixed(0)}%`);
        rect.addEventListener("click", () => focusObject(rowFile.path, "file"));
        if (pair.together >= maximum * 0.4) svgNode(svg, "text", { x: x + cell / 2, y: y + cell / 2 + 3.5, "text-anchor": "middle", class: "heat-value" }, pair.together);
      }
    });
  });
  svgNode(svg, "text", { x: 4, y: height - 6, class: "axis-label" }, `darker = more shared commits · max ${maximum}`);
  pairs.slice(0, 5).forEach(pair => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "pair-row";
    row.title = `${pair.a} ↔ ${pair.b}`;
    row.innerHTML = `<span class="pair-names">${escapeHtml(shortPath(pair.a, 20))} ↔ ${escapeHtml(shortPath(pair.b, 20))}</span><span class="pair-count">${pair.together} · lift ${pair.lift.toFixed(1)}×</span>`;
    row.addEventListener("click", () => focusObject(pair.a, "file"));
    pairsBox.appendChild(row);
  });
}

function renderTable(items) {
  $("#metrics-body").innerHTML = items.map(item => `<tr data-path="${escapeHtml(item.path)}" data-kind="${item.kind}">
    <td class="path-cell" title="${escapeHtml(item.path)}">${escapeHtml(item.path)}</td><td><span class="kind-tag">${item.kind}</span></td>
    <td class="positive">+${number.format(item.added)}</td><td class="negative">−${number.format(item.removed)}</td><td class="${item.growth < 0 ? "negative" : "positive"}">${item.growth > 0 ? "+" : ""}${number.format(item.growth)}</td><td>${number.format(item.churn)}</td><td>${number.format(item.modifications)}</td><td>${(item.modification_frequency * 100).toFixed(1)}%</td><td>${item.churn_rate.toFixed(2)}</td>
  </tr>`).join("");
  $("#metrics-body").querySelectorAll("tr").forEach(row => row.addEventListener("click", () => focusObject(row.dataset.path, row.dataset.kind)));
}

function openRepositoryDialog() { $("#repository-dialog").showModal(); }
$("#add-repo-button").addEventListener("click", openRepositoryDialog);
document.querySelectorAll("[data-open-dialog]").forEach(button => button.addEventListener("click", openRepositoryDialog));
$("#refresh-button").addEventListener("click", () => loadRepositories(state.selectedRepository));
$("#delete-repo-button").addEventListener("click", async () => {
  const repo = state.repositories.find(item => item.id === state.selectedRepository);
  if (!repo) return;
  if (!confirm(`Delete "${repo.name}"? This removes all data and cannot be undone.`)) return;
  try {
    await api(`/api/repositories/${repo.id}`, { method: "DELETE" });
    toast(`"${repo.name}" deleted.`);
    state.selectedRepository = null;
    await loadRepositories();
  } catch (error) { toast(error.message); }
});
$("#repository-select").addEventListener("change", event => { state.selectedRepository = Number(event.target.value); loadRepositoryData(); });
$("#apply-filters").addEventListener("click", loadAnalytics);
$("#bucket-toggle").addEventListener("click", event => {
  const button = event.target.closest("button[data-bucket]");
  if (!button) return;
  state.bucket = button.dataset.bucket;
  document.querySelectorAll("#bucket-toggle button").forEach(node => node.classList.toggle("active", node === button));
  fetchTimeline();
});
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
