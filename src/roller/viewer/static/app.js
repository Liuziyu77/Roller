(() => {
  "use strict";

  const state = {
    run: null,
    tasks: null,
    page: 1,
    pageSize: 80,
    query: "",
    status: "",
    model: "",
    selectedId: "",
    detail: null,
    steps: new Map(),
    expanded: new Set(),
    inspectorTab: "overview",
  };

  const elements = {
    runName: document.querySelector("#run-name"),
    runPath: document.querySelector("#run-path"),
    metrics: document.querySelector("#metrics"),
    refresh: document.querySelector("#refresh-button"),
    search: document.querySelector("#search-input"),
    status: document.querySelector("#status-filter"),
    model: document.querySelector("#model-filter"),
    taskRange: document.querySelector("#task-range"),
    taskCount: document.querySelector("#task-count"),
    taskList: document.querySelector("#task-list"),
    pagination: document.querySelector("#pagination"),
    timelinePanel: document.querySelector("#timeline-panel"),
    timelineEmpty: document.querySelector("#timeline-empty"),
    timeline: document.querySelector("#timeline-content"),
    inspectorPanel: document.querySelector("#inspector-panel"),
    inspector: document.querySelector("#inspector-content"),
    toast: document.querySelector("#toast"),
    error: document.querySelector("#error-banner"),
  };

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  const jsonText = (value) => {
    try { return JSON.stringify(value, null, 2); }
    catch { return String(value); }
  };

  const statusClass = (value) => ["ok", "skipped", "error", "timeout", "partial"].includes(value)
    ? value
    : "partial";

  const formatNumber = (value) => {
    const number = Number(value);
    return Number.isFinite(number) ? new Intl.NumberFormat("zh-CN").format(number) : "—";
  };

  const formatDuration = (value) => {
    const seconds = Number(value);
    if (!Number.isFinite(seconds)) return "—";
    if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
    if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 2 : 1)} s`;
    const minutes = Math.floor(seconds / 60);
    const rest = Math.round(seconds % 60);
    return `${minutes}m ${rest}s`;
  };

  const api = async (path) => {
    const response = await fetch(path, { headers: { Accept: "application/json" } });
    let payload;
    try { payload = await response.json(); }
    catch { throw new Error(`Viewer API 返回了无效 JSON（HTTP ${response.status}）`); }
    if (!response.ok) throw new Error(payload.error || response.statusText);
    return payload;
  };

  const showError = (error) => {
    elements.error.textContent = error instanceof Error ? error.message : String(error);
    elements.error.hidden = false;
    window.setTimeout(() => { elements.error.hidden = true; }, 8000);
  };

  let toastTimer = null;
  const toast = (message) => {
    elements.toast.textContent = message;
    elements.toast.classList.add("show");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => elements.toast.classList.remove("show"), 1800);
  };

  const copyText = async (text) => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.style.position = "fixed";
      textarea.style.left = "-9999px";
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      textarea.remove();
    }
    toast("已复制");
  };

  const metric = (label, value, note = "") => `
    <div class="metric">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
      ${note ? `<small>${escapeHtml(note)}</small>` : ""}
    </div>`;

  const renderRun = () => {
    const run = state.run;
    if (!run) return;
    elements.runName.textContent = run.name || "Trajectory Viewer";
    elements.runPath.textContent = run.path || "";
    elements.runPath.title = run.path || "";
    const counts = run.status_counts || {};
    const completed = Number(counts.ok || 0) + Number(counts.skipped || 0);
    const failed = Number(counts.error || 0) + Number(counts.timeout || 0);
    const usage = run.usage || {};
    const progress = run.progress || {};
    const thinkingNote = run.configuration?.thinking || "model default";
    elements.metrics.innerHTML = [
      metric(
        "Run",
        run.state || (run.finished_at ? "finished" : "unknown"),
        progress.total == null ? "no checkpoint" : `${formatNumber(progress.processed || 0)} / ${formatNumber(progress.total)} processed`,
      ),
      metric("Tasks", formatNumber(run.task_count), "unique task IDs"),
      metric("Rollouts", formatNumber(run.rollout_count), `${formatNumber(run.models?.length || 0)} models`),
      metric("Completed", formatNumber(completed), counts.skipped ? `${counts.skipped} resumed` : "ok"),
      metric("Failed", formatNumber(failed), counts.timeout ? `${counts.timeout} timeout` : "error + timeout"),
      metric("Tokens", formatNumber(usage.total_tokens), `${formatNumber(usage.request_count)} requests`),
      metric(
        "Thinking",
        run.configuration?.keep_thinking === true
          ? "kept"
          : run.configuration?.keep_thinking === false ? "stripped" : "unknown",
        thinkingNote,
      ),
    ].join("");

    const currentStatus = state.status;
    elements.status.innerHTML = '<option value="">全部状态</option>' + Object.keys(counts)
      .sort()
      .map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)} (${counts[value]})</option>`)
      .join("");
    elements.status.value = currentStatus;
    const currentModel = state.model;
    elements.model.innerHTML = '<option value="">全部模型</option>' + (run.models || [])
      .map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`)
      .join("");
    elements.model.value = currentModel;
  };

  const rolloutMeta = (rollout) => {
    const tokens = rollout.usage?.total_tokens;
    return tokens ? `${formatNumber(tokens)} tok` : rollout.has_actf ? "ACTF" : "no trace";
  };

  const renderTasks = () => {
    const payload = state.tasks;
    if (!payload) return;
    elements.taskCount.textContent = formatNumber(payload.total);
    const start = payload.total ? (payload.page - 1) * payload.page_size + 1 : 0;
    const end = Math.min(payload.total, payload.page * payload.page_size);
    elements.taskRange.textContent = payload.total ? `${start}–${end} / ${payload.total} tasks` : "没有匹配的 task";
    if (!payload.items.length) {
      elements.taskList.innerHTML = '<div class="empty-list">没有符合当前搜索和筛选条件的任务。</div>';
    } else {
      elements.taskList.innerHTML = payload.items.map((task) => `
        <article class="task-group">
          <div class="task-title">
            <span class="task-index">${escapeHtml(task.source_index ?? "—")}</span>
            <strong title="${escapeHtml(task.task_id)}">${escapeHtml(task.task_id)}</strong>
          </div>
          ${task.rollouts.map((rollout) => `
            <button class="rollout-row ${state.selectedId === rollout.id ? "selected" : ""}" data-rollout="${rollout.id}" type="button">
              <span class="status-dot ${statusClass(rollout.status)}" title="${escapeHtml(rollout.status)}"></span>
              <span class="rollout-model" title="${escapeHtml(rollout.model)}">${escapeHtml(rollout.model || "unknown model")}</span>
              <span class="rollout-meta">${escapeHtml(rolloutMeta(rollout))}</span>
            </button>`).join("")}
        </article>`).join("");
    }
    const pageCount = Math.max(1, Math.ceil(payload.total / payload.page_size));
    elements.pagination.innerHTML = `
      <button type="button" data-page="${payload.page - 1}" ${payload.page <= 1 ? "disabled" : ""}>← 上一页</button>
      <span>${payload.page} / ${pageCount}</span>
      <button type="button" data-page="${payload.page + 1}" ${payload.page >= pageCount ? "disabled" : ""}>下一页 →</button>`;
  };

  const stepHeadline = (step) => step.content_preview || step.reasoning_preview || step.user_preview || (step.tool_call_count ? "工具调用" : "空内容");

  const stepTags = (step) => {
    const tags = [];
    if (step.reasoning_chars) tags.push(`<i class="reasoning">reasoning ${formatNumber(step.reasoning_chars)}</i>`);
    if (step.tool_call_count) tags.push(`<i class="tools">${step.tool_call_count} tools</i>`);
    if (step.has_tool_error) tags.push('<i class="error">tool error</i>');
    const input = step.metric?.prompt_tokens_len;
    const output = step.metric?.completion_tokens_len;
    if (input || output) tags.push(`<i>${formatNumber(input || 0)} / ${formatNumber(output || 0)} tok</i>`);
    if (step.duration_seconds != null) tags.push(`<i>${escapeHtml(formatDuration(step.duration_seconds))}</i>`);
    return tags.join("");
  };

  const truncation = (view, index) => view?.truncated ? `
    <div class="truncation-note">
      <span>当前显示 ${formatNumber(view.text.length)} / ${formatNumber(view.length)} 字符</span>
      <button type="button" data-full-step="${index}">加载更多</button>
    </div>` : "";

  const textSection = (kind, title, view, index) => {
    if (!view?.text) return "";
    if (kind === "reasoning") {
      return `<section class="trace-section reasoning">
        <header><span>${escapeHtml(title)}</span><span>${formatNumber(view.length)} chars</span></header>
        <details data-reasoning>
          <summary>展开思考内容</summary>
          <pre>${escapeHtml(view.text)}</pre>
          ${truncation(view, index)}
        </details>
      </section>`;
    }
    return `<section class="trace-section ${kind}">
      <header><span>${escapeHtml(title)}</span><button type="button" data-copy="${kind}">COPY</button></header>
      <pre>${escapeHtml(view.text)}</pre>
      ${truncation(view, index)}
    </section>`;
  };

  const observationSection = (observation, index) => `
    <section class="trace-section tool-result ${observation.is_error ? "error" : ""}">
      <header>
        <span>${observation.is_error ? "Tool Error" : "Tool Result"} · ${escapeHtml(observation.tool_name || observation.tool_call_id || "unknown")}</span>
        <span>${formatNumber(observation.content?.length || 0)} chars</span>
      </header>
      ${observation.content?.blocks
        ? observation.content.blocks.map((block) => block.type === "image"
          ? `<img class="tool-image" src="${escapeHtml(block.url)}" alt="Tool result image" loading="lazy">`
          : `<pre>${escapeHtml(block.text)}</pre>`).join("")
        : `<pre>${escapeHtml(observation.content?.text || "")}</pre>`}
      ${truncation(observation.content, index)}
    </section>`;

  const renderStepBody = (step, index) => {
    const observations = [...(step.observations || [])];
    const toolHtml = (step.assistant?.tool_calls || []).map((call) => {
      const matches = observations.filter((item) => item.tool_call_id && item.tool_call_id === call.id);
      for (const match of matches) observations.splice(observations.indexOf(match), 1);
      return `<div>
        <section class="trace-section tool-call">
          <header><span>Tool Call · ${escapeHtml(call.name || "unknown")}</span><span>${escapeHtml(call.id || "")}</span></header>
          <pre>${escapeHtml(call.arguments?.text || "{}")}</pre>
          ${truncation(call.arguments, index)}
        </section>
        ${matches.map((item) => observationSection(item, index)).join("")}
      </div>`;
    }).join("");
    return [
      textSection("user", "User", step.user, index),
      textSection("reasoning", "Reasoning", step.assistant?.reasoning, index),
      toolHtml,
      observations.map((item) => observationSection(item, index)).join(""),
      textSection("assistant", "Assistant Content", step.assistant?.content, index),
    ].filter(Boolean).join("") || '<div class="empty-list">该 step 没有可展示的文本或工具事件。</div>';
  };

  const renderTimeline = () => {
    const detail = state.detail;
    if (!detail) {
      elements.timelineEmpty.hidden = false;
      elements.timeline.hidden = true;
      return;
    }
    elements.timelineEmpty.hidden = true;
    elements.timeline.hidden = false;
    const rollout = detail.rollout;
    const trajectory = detail.trajectory;
    const usage = rollout.usage || {};
    const header = `
      <header class="trajectory-header">
        <div class="trajectory-title-row">
          <div>
            <span class="eyebrow">${escapeHtml(rollout.model || "ROLLOUT")}</span>
            <h2>${escapeHtml(rollout.task_id)}</h2>
            <p>${escapeHtml(rollout.path)}</p>
          </div>
          <span class="status-chip ${statusClass(rollout.status)}">${escapeHtml(rollout.status)}</span>
        </div>
        <div class="trajectory-stats">
          <span>${formatNumber(trajectory?.step_count || 0)} steps</span>
          <span>${formatNumber(usage.input_tokens)} input</span>
          <span>${formatNumber(usage.output_tokens)} output</span>
          <span>${formatNumber(usage.cache_read_tokens)} cache read</span>
          <span>${escapeHtml(formatDuration(rollout.timing?.elapsed_seconds ?? usage.elapsed_seconds))}</span>
        </div>
        <div class="trajectory-actions">
          <button type="button" data-action="collapse">折叠全部</button>
          <button type="button" data-action="reasoning">展开/收起 reasoning</button>
          <button type="button" data-action="inspector">Inspector</button>
        </div>
      </header>`;
    if (!trajectory) {
      elements.timeline.innerHTML = header + `
        <div class="empty-state no-actf">
          <p class="eyebrow">NO ACTF</p>
          <h2>该 rollout 没有可用的 ACTF 轨迹</h2>
          <p>${escapeHtml(rollout.error || "sample 尚未完成，或轨迹转换未成功。")}</p>
        </div>`;
      return;
    }
    const steps = trajectory.steps || [];
    const scrollTop = elements.timelinePanel.scrollTop;
    elements.timeline.innerHTML = header + `<ol class="step-list">${steps.map((step) => {
      const expanded = state.expanded.has(step.index);
      const loaded = state.steps.get(step.index);
      return `<li class="step-card ${expanded ? "expanded" : ""}">
        <button class="step-toggle" type="button" data-step="${step.index}" aria-expanded="${expanded}">
          <span class="step-number">${escapeHtml(step.step_id)}</span>
          <span class="step-summary">
            <strong>${escapeHtml(stepHeadline(step))}</strong>
            <span>${escapeHtml(step.metric?.stop_reason || "step")} · ${escapeHtml(step.started_at || "")}</span>
          </span>
          <span class="step-tags">${stepTags(step)}<b class="step-chevron">⌄</b></span>
        </button>
        ${expanded ? `<div class="step-body">${loaded ? renderStepBody(loaded, step.index) : '<div class="loading-step">正在加载 step 内容…</div>'}</div>` : ""}
      </li>`;
    }).join("")}</ol>`;
    elements.timelinePanel.scrollTop = scrollTop;
  };

  const infoRows = (rollout) => {
    const usage = rollout.usage || {};
    return [
      ["Task", rollout.task_id], ["Model", rollout.model], ["Status", rollout.status],
      ["Harness", rollout.harness || "—"], ["Request", rollout.request_index ?? "—"],
      ["Attempts", rollout.attempt_count], ["Total tokens", formatNumber(usage.total_tokens)],
      ["Elapsed", formatDuration(rollout.timing?.elapsed_seconds ?? usage.elapsed_seconds)],
      ["ACTF", rollout.has_actf ? "available" : "missing"], ["Path", rollout.path],
    ];
  };

  const conversationHtml = (conversation) => {
    if (!conversation) return '<div class="empty-list">该 task 没有 User Simulator conversation。</div>';
    const rounds = Array.isArray(conversation.rounds) ? conversation.rounds : [];
    return `
      <dl class="info-table">
        <dt>Status</dt><dd>${escapeHtml(conversation.status || "—")}</dd>
        <dt>Termination</dt><dd>${escapeHtml(conversation.termination?.reason || "—")}</dd>
        <dt>Rounds</dt><dd>${formatNumber(rounds.length)}</dd>
      </dl>
      <h3>Visible conversation</h3>
      ${rounds.map((round, index) => `<div class="conversation-round">
        <strong>Round ${index + 1}</strong>
        <pre class="code-block">${escapeHtml(jsonText(round))}</pre>
      </div>`).join("") || '<div class="empty-list">没有已完成的对话轮次。</div>'}`;
  };

  const renderInspector = () => {
    const detail = state.detail;
    if (!detail) {
      elements.inspector.className = "inspector-empty";
      elements.inspector.textContent = "选择 rollout 后可查看运行信息、system prompt、tools schema 和原始 metadata。";
      return;
    }
    const tabs = [
      ["overview", "Overview"], ["system", "System"], ["tools", "Tools"], ["json", "JSON"],
    ];
    if (detail.conversation) tabs.splice(1, 0, ["conversation", "Conversation"]);
    if (!tabs.some(([key]) => key === state.inspectorTab)) state.inspectorTab = "overview";
    let body = "";
    if (state.inspectorTab === "overview") {
      body = `<dl class="info-table">${infoRows(detail.rollout).map(([key, value]) => `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd>`).join("")}</dl>`;
      if (detail.rollout.error) body += `<h3>Error</h3><pre class="code-block inspector-code">${escapeHtml(detail.rollout.error)}</pre>`;
    } else if (state.inspectorTab === "conversation") {
      body = detail.conversation_error
        ? `<div class="empty-list">${escapeHtml(detail.conversation_error)}</div>`
        : conversationHtml(detail.conversation);
    } else if (state.inspectorTab === "system") {
      const prompt = detail.trajectory?.system_prompt?.text || "";
      body = prompt ? `<pre class="code-block inspector-code">${escapeHtml(prompt)}</pre>` : '<div class="empty-list">未捕获 system prompt。</div>';
    } else if (state.inspectorTab === "tools") {
      const tools = detail.trajectory?.tools || [];
      body = tools.length ? `<pre class="code-block inspector-code">${escapeHtml(jsonText(tools))}</pre>` : '<div class="empty-list">未捕获 tools schema。</div>';
    } else {
      body = `<pre class="code-block inspector-code">${escapeHtml(jsonText(detail.info))}</pre>`;
    }
    elements.inspector.className = "inspector-wrap";
    elements.inspector.innerHTML = `
      <nav class="tabs">${tabs.map(([key, label]) => `<button type="button" data-tab="${key}" class="${state.inspectorTab === key ? "active" : ""}">${label}</button>`).join("")}</nav>
      <div class="inspector-body">${body}</div>`;
  };

  const loadTasks = async () => {
    elements.taskList.innerHTML = '<div class="skeleton-list"><i></i><i></i><i></i><i></i></div>';
    const params = new URLSearchParams({ page: String(state.page), page_size: String(state.pageSize) });
    if (state.query) params.set("query", state.query);
    if (state.status) params.set("status", state.status);
    if (state.model) params.set("model", state.model);
    state.tasks = await api(`/api/v1/tasks?${params}`);
    renderTasks();
  };

  const selectRollout = async (rolloutId, updateHash = true) => {
    if (!rolloutId) return;
    state.selectedId = rolloutId;
    state.detail = null;
    state.steps.clear();
    state.expanded.clear();
    state.inspectorTab = "overview";
    renderTasks();
    elements.timelineEmpty.hidden = true;
    elements.timeline.hidden = false;
    elements.timeline.innerHTML = '<div class="empty-state"><p class="eyebrow">LOADING</p><h2>正在读取 ACTF 轨迹…</h2></div>';
    try {
      state.detail = await api(`/api/v1/rollouts/${encodeURIComponent(rolloutId)}`);
      if (updateHash) history.replaceState(null, "", `#/rollout/${encodeURIComponent(rolloutId)}`);
      elements.timelinePanel.scrollTop = 0;
      renderTimeline();
      renderInspector();
    } catch (error) {
      showError(error);
      state.selectedId = "";
      renderTimeline();
    }
  };

  const loadStep = async (index, limit = 40_000) => {
    try {
      const step = await api(`/api/v1/rollouts/${encodeURIComponent(state.selectedId)}/steps/${index}?limit=${limit}`);
      state.steps.set(index, step);
      renderTimeline();
    } catch (error) {
      state.expanded.delete(index);
      renderTimeline();
      showError(error);
    }
  };

  const toggleStep = (index) => {
    if (state.expanded.has(index)) {
      state.expanded.delete(index);
      renderTimeline();
      return;
    }
    state.expanded.add(index);
    renderTimeline();
    if (!state.steps.has(index)) void loadStep(index);
  };

  const loadRun = async (refresh = false) => {
    elements.refresh.classList.add("busy");
    try {
      state.run = await api(`/api/v1/run${refresh ? "?refresh=1" : ""}`);
      renderRun();
      await loadTasks();
      if (refresh) toast("Run 索引已刷新");
    } catch (error) {
      showError(error);
    } finally {
      elements.refresh.classList.remove("busy");
    }
  };

  elements.taskList.addEventListener("click", (event) => {
    const button = event.target.closest("[data-rollout]");
    if (button) void selectRollout(button.dataset.rollout);
  });

  elements.pagination.addEventListener("click", (event) => {
    const button = event.target.closest("[data-page]");
    if (!button || button.disabled) return;
    state.page = Number(button.dataset.page);
    void loadTasks();
  });

  elements.timeline.addEventListener("click", (event) => {
    const stepButton = event.target.closest("[data-step]");
    if (stepButton) {
      toggleStep(Number(stepButton.dataset.step));
      return;
    }
    const fullButton = event.target.closest("[data-full-step]");
    if (fullButton) {
      void loadStep(Number(fullButton.dataset.fullStep), 1_000_000);
      return;
    }
    const action = event.target.closest("[data-action]")?.dataset.action;
    if (action === "collapse") {
      state.expanded.clear();
      renderTimeline();
    } else if (action === "reasoning") {
      const details = [...elements.timeline.querySelectorAll("details[data-reasoning]")];
      const shouldOpen = details.some((item) => !item.open);
      details.forEach((item) => { item.open = shouldOpen; });
    } else if (action === "inspector") {
      elements.inspectorPanel.classList.toggle("open");
    }
    const copyButton = event.target.closest("[data-copy]");
    if (copyButton) {
      const pre = copyButton.closest(".trace-section")?.querySelector("pre");
      if (pre) void copyText(pre.textContent || "");
    }
  });

  elements.inspector.addEventListener("click", (event) => {
    const button = event.target.closest("[data-tab]");
    if (!button) return;
    state.inspectorTab = button.dataset.tab;
    renderInspector();
  });

  let searchTimer = null;
  elements.search.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => {
      state.query = elements.search.value.trim();
      state.page = 1;
      void loadTasks();
    }, 220);
  });
  elements.status.addEventListener("change", () => {
    state.status = elements.status.value;
    state.page = 1;
    void loadTasks();
  });
  elements.model.addEventListener("change", () => {
    state.model = elements.model.value;
    state.page = 1;
    void loadTasks();
  });
  elements.refresh.addEventListener("click", () => void loadRun(true));

  const routeRollout = () => {
    const match = location.hash.match(/^#\/rollout\/([^/?]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  };

  window.addEventListener("hashchange", () => {
    const rolloutId = routeRollout();
    if (rolloutId && rolloutId !== state.selectedId) void selectRollout(rolloutId, false);
  });

  void (async () => {
    await loadRun(false);
    const rolloutId = routeRollout();
    if (rolloutId) await selectRollout(rolloutId, false);
  })();
})();
