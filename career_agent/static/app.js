const state = { kind: "chat", username: null, modelReady: true, ids: { chat: null, interview: null }, busy: { chat: false, interview: false }, pending: { chat: null, interview: null }, sessionLists: { chat: [], interview: [] }, sessionQueries: { chat: "", interview: "" }, sessionSearchSeq: { chat: 0, interview: 0 }, selectionSeq: 0, chatSeq: 0, interviewSeq: 0, interviewQuestion: 0, editing: null };
const $ = (id) => document.getElementById(id);
const sessionKey = (kind) => `${kind}Session${state.username ? `:${state.username}` : ""}`;
let sessionSearchTimer;

function showAuth() {
  clearTimeout(sessionSearchTimer);
  state.selectionSeq++;
  $("shell").hidden = true;
  $("auth-panel").hidden = false;
  for (const kind of ["chat", "interview"]) state.pending[kind]?.controller.abort();
  state.pending = { chat: null, interview: null };
  state.chatSeq++;
  state.interviewSeq++;
  setBusy("chat", false);
  setBusy("interview", false);
  state.ids = { chat: null, interview: null };
  state.sessionLists = { chat: [], interview: [] };
  state.sessionQueries = { chat: "", interview: "" };
  state.sessionSearchSeq.chat++;
  state.sessionSearchSeq.interview++;
  state.username = null;
  for (const id of ["chat-form", "answer-form", "material-form", "claim-form", "interview-setup", "chat-memory-form"]) $(id).reset();
  if ($("resource-editor").open) $("resource-editor").close();
  $("chat-resource").replaceChildren(new Option("选择要讨论的内容", ""));
  $("material-list").replaceChildren();
  $("claim-list").replaceChildren();
  $("claim-source").replaceChildren(new Option("无关联材料", ""));
  renderSelectedResources(null);
  $("session-search").value = "";
  $("session-count").textContent = "";
  $("workspace-title").textContent = "新对话";
  $("chat-notes").hidden = true;
  $("chat-notes").open = false;
  $("chat-export").hidden = true;
  $("chat-retry").hidden = true;
  $("chat-interrupt").hidden = true;
  $("rename-session").hidden = true;
  $("delete-session").hidden = true;
  notice();
  $("session-title-form").hidden = true;
  setHistoryOpen(false);
  $("sessions").replaceChildren();
  renderMessages("chat", []);
  renderMessages("interview", []);
  renderRunHistory("chat", [], false);
  renderRunHistory("interview", [], false);
  renderReport(null);
  renderInterviewState(null);
}

function showApp(username = null) {
  state.username = username;
  state.ids = { chat: localStorage.getItem(sessionKey("chat")), interview: localStorage.getItem(sessionKey("interview")) };
  $("auth-panel").hidden = true;
  $("shell").hidden = false;
}

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, { headers: { "Content-Type": "application/json" }, ...options });
  const data = await response.json().catch(() => null);
  if (response.status === 401 && !path.startsWith("/auth/")) showAuth();
  if (!response.ok) {
    const detail = data?.detail;
    const message = typeof detail === "string" ? detail
      : Array.isArray(detail) ? "输入不符合要求，请检查必填项、长度和格式。" : `请求失败 (${response.status})`;
    throw new Error(message);
  }
  return data;
}

function notice(message = "") { $("notice").textContent = message; }
function setHistoryOpen(open) {
  $("sidebar-history").classList.toggle("open", open);
  $("history-toggle").setAttribute("aria-expanded", String(open));
  $("history-toggle").textContent = `${open ? "收起" : "展开"}${state.kind === "chat" ? "历史对话" : "历史面试"}`;
}
async function copyId(id) {
  try { await navigator.clipboard.writeText(id); notice("ID 已复制"); }
  catch { notice(`ID：${id}`); }
}
function setBusy(kind, value) {
  state.busy[kind] = value;
  document.querySelectorAll(`#${kind === "chat" ? "chat-panel" : "interview-panel"} button[type=submit]`).forEach((button) => {
    button.disabled = value || (!state.modelReady && Boolean(button.closest("#chat-form, #interview-setup, #answer-form")));
  });
}
async function loadChatResources() {
  const sequence = state.selectionSeq;
  const [materials, sessions] = await Promise.all([api("/materials"), api("/interview/sessions")]);
  if (sequence !== state.selectionSeq || state.kind !== "chat") return;
  const select = $("chat-resource");
  select.replaceChildren(new Option("选择要讨论的内容", ""));
  for (const material of materials) {
    const option = new Option(`材料 · ${material.title}`, material.id);
    option.dataset.kind = "material";
    select.append(option);
  }
  for (const session of sessions.filter((item) => ["finished", "cancelled"].includes(item.status) && item.has_feedback)) {
    const option = new Option(`面试反馈 · ${session.title || session.goal}`, session.id);
    option.dataset.kind = "interview";
    select.append(option);
  }
  $("chat-insert-resource").disabled = true;
}
function insertChatReference(id, kind) {
  const input = $("chat-text");
  const existing = input.value.trim();
  const prompt = kind === "material"
    ? existing ? `请结合材料 ${id} 回答。` : `请分析材料 ${id} 的重点。`
    : existing ? `请结合面试 ${id} 的反馈回答。` : `请解释面试 ${id} 的反馈，并给我复练建议。`;
  input.value = existing ? `${existing}\n\n${prompt}` : prompt;
  input.focus();
}
function renderMessages(kind, messages) {
  const box = $(`${kind}-messages`);
  box.replaceChildren();
  if (!messages.length) {
    if (kind === "chat") {
      const empty = document.createElement("div");
      empty.className = "chat-empty";
      const heading = document.createElement("h2");
      heading.textContent = "从一个问题开始";
      const description = document.createElement("p");
      description.textContent = "整理岗位要求、打磨经历表达，或讨论刚结束的面试反馈。";
      const starters = document.createElement("div");
      starters.className = "chat-starters";
      for (const [label, prompt] of [
        ["分析目标岗位", "帮我梳理目标岗位最看重的能力，以及我应该先准备什么。"],
        ["打磨项目经历", "我想把一段项目经历讲清楚。请先问我做了什么、为什么做、结果如何。"],
        ["规划面试准备", "根据我的目标岗位，帮我列一个可执行的面试准备顺序。"],
      ]) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "chat-starter";
        button.textContent = label;
        button.onclick = () => { $("chat-text").value = prompt; $("chat-text").focus(); };
        starters.append(button);
      }
      empty.append(heading, description, starters);
      box.append(empty);
    } else {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "设置目标岗位后开始面试。";
      box.append(empty);
    }
  }
  for (const message of messages) {
    const item = document.createElement("div");
    item.className = `message ${message.role}`;
    const role = document.createElement("span");
    role.className = "role";
    role.textContent = message.role === "user" ? "你" : kind === "chat" ? "聊天助手" : "面试官";
    item.append(role, document.createTextNode(message.content === "[SKIPPED_QUESTION]" ? "已跳过本题" : message.content));
    if (kind === "chat" && message.role === "assistant") {
      const save = document.createElement("button");
      save.type = "button";
      save.className = "save-claim secondary";
      save.textContent = "存为经历草稿";
      save.onclick = async () => {
        const draft = window.prompt("编辑经历草稿；保存后仍需在面试准备区确认真实", message.content.slice(0, 500));
        if (!draft?.trim()) return;
        try {
          await api("/claims", { method: "POST", body: JSON.stringify({ text: draft.trim() }) });
          notice("经历草稿已保存，请到面试准备区确认真实。");
        } catch (error) { notice(error.message); }
      };
      item.append(save);
    }
    box.append(item);
  }
  box.scrollTop = box.scrollHeight;
}

function renderRunHistory(kind, events, hasSession) {
  const panel = $(`${kind}-run-history`);
  const box = $(`${kind}-run-events`);
  panel.hidden = !hasSession;
  box.replaceChildren();
  if (!hasSession) return;
  const recent = events.filter((event) => ["model_run", "tool"].includes(event.kind)).slice(-30).reverse();
  panel.querySelector("summary").textContent = `运行记录（最近 ${recent.length} 条）`;
  if (!recent.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "还没有模型或工具运行记录。";
    box.append(empty);
    return;
  }
  for (const event of recent) {
    const detail = JSON.parse(event.detail);
    const item = document.createElement("div");
    item.className = "run-event";
    const title = document.createElement("strong");
    const lines = [];
    if (event.kind === "tool") {
      title.textContent = `工具 ${detail.name || "未知"} · ${detail.result === "ok" ? "成功" : detail.result || "未知"}`;
    } else {
      const status = { completed: "完成", failed: "失败", interrupted: "已中断", cancelled: "已取消" };
      title.textContent = `模型运行 · ${status[detail.status] || detail.status || "未知"}`;
      lines.push(`${detail.provider || "未知模型"}${detail.model ? ` / ${detail.model}` : ""} · ${detail.elapsed_ms ?? "未知"} 毫秒 · ${detail.usage?.request_count ?? "未知"} 次请求`);
      if (detail.usage_coverage === "unavailable" || typeof detail.usage?.total_tokens !== "number") {
        lines.push("令牌用量：不可用");
      } else {
        lines.push(`令牌用量：${detail.usage.total_tokens}（输入 ${detail.usage.prompt_tokens}、输出 ${detail.usage.completion_tokens}）${detail.usage_coverage === "partial" ? "；部分请求未返回用量" : ""}`);
      }
      lines.push(`初始上下文：${detail.context_chars} 字符 · 提示版本：${detail.prompt_version || "未知"}`);
      if (detail.error_code) lines.push(`错误码：${detail.error_code}`);
    }
    const time = document.createElement("small");
    time.textContent = new Date(event.created_at).toLocaleString();
    item.append(title, time);
    for (const line of lines) {
      const text = document.createElement("p");
      text.textContent = line;
      item.append(text);
    }
    box.append(item);
  }
}

function renderInterviewState(session) {
  state.interviewQuestion = session?.question_count || 0;
  $("copy-interview-id").hidden = !session;
  $("interview-export").hidden = !session;
  if (session) $("interview-export").href = `/api/interview/sessions/${session.id}/export.md`;
  $("pause-running").hidden = !session || session.status !== "running" || session.question_count > 0;
  $("cancel-interview").hidden = !session || ["finished", "cancelled"].includes(session.status);
  $("preparation").hidden = Boolean(session);
  $("interview-setup").hidden = Boolean(session);
  $("answer-form").hidden = !session || !(session.status === "awaiting_answer" ||
    (session.status === "running" && session.question_count > 0));
  $("answer-form").querySelector("button[type=submit]").textContent = session?.status === "running" ? "提交修正" : "提交回答";
  $("skip-question").hidden = !session || session.status !== "awaiting_answer";
  $("skip-question").disabled = !state.modelReady;
  $("resume-actions").hidden = !session || session.status !== "paused";
  $("resume-button").disabled = !state.modelReady;
  const names = { new: "尚未开始", running: "正在生成回复", awaiting_answer: `等待回答 · 第 ${session?.question_count ?? 0} 题`, paused: "已暂停", finished: "已完成", cancelled: "已结束" };
  $("interview-status").textContent = session ? `${session.goal} · ${names[session.status]} / 共 ${session.max_questions} 题` : "创建一场面试";
}

function openResourceEditor(kind, resource) {
  state.editing = { kind, id: resource.id };
  const material = kind === "material";
  $("resource-editor-heading").textContent = material ? "编辑材料" : "编辑经历";
  $("resource-edit-title-label").hidden = !material;
  $("resource-edit-title").hidden = !material;
  $("resource-edit-title").disabled = !material;
  $("resource-edit-title").value = material ? resource.title : "";
  $("resource-edit-content-label").textContent = material ? "材料内容" : "经历主张（保存后需要重新确认）";
  $("resource-edit-content").maxLength = material ? 4000 : 500;
  $("resource-edit-content").value = material ? resource.content : resource.text;
  $("resource-edit-error").hidden = true;
  $("resource-editor").showModal();
}

async function loadPreparation() {
  const sequence = state.selectionSeq;
  const [materials, claims] = await Promise.all([api("/materials"), api("/claims")]);
  if (sequence !== state.selectionSeq || state.kind !== "interview") return;
  const materialBox = $("material-list");
  const claimBox = $("claim-list");
  const selectedMaterials = new Set([...materialBox.querySelectorAll("input:checked")].map((item) => item.value));
  const selectedClaims = new Set([...claimBox.querySelectorAll("input:checked")].map((item) => item.value));
  materialBox.replaceChildren();
  claimBox.replaceChildren();
  const source = $("claim-source");
  const priorSource = source.value;
  source.replaceChildren(new Option("无关联材料", ""));
  const kinds = { resume: "简历", job_description: "岗位描述", project: "项目材料" };
  for (const material of materials) {
    const row = document.createElement("div");
    row.className = "resource-row";
    const selection = document.createElement("label");
    selection.className = "resource-selection";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox"; checkbox.value = material.id; checkbox.checked = selectedMaterials.has(material.id);
    const text = document.createElement("span");
    text.textContent = `${kinds[material.kind]} · ${material.title}`;
    selection.append(checkbox, text);
    row.append(selection);
    const copy = document.createElement("button");
    copy.type = "button"; copy.className = "secondary"; copy.textContent = "复制 ID";
    copy.onclick = () => copyId(material.id);
    row.append(copy);
    const edit = document.createElement("button");
    edit.type = "button"; edit.className = "secondary"; edit.textContent = "编辑";
    edit.onclick = () => openResourceEditor("material", material);
    row.append(edit);
    const remove = document.createElement("button");
    remove.type = "button"; remove.className = "secondary"; remove.textContent = "撤回";
    remove.onclick = () => {
      if (!window.confirm(`撤回“${material.title}”及其关联经历？旧面试记录会保留。`)) return;
      action("interview", async () => {
        await api(`/materials/${material.id}`, { method: "DELETE" });
        await loadPreparation();
        notice("材料及其关联经历已撤回。");
      });
    };
    row.append(remove);
    materialBox.append(row);
    if (material.kind !== "job_description") source.append(new Option(material.title, material.id));
  }
  source.value = priorSource;
  for (const claim of claims) {
    const row = document.createElement("div");
    row.className = "resource-row";
    const text = document.createElement("span");
    text.textContent = `${claim.status === "confirmed" ? "已确认" : "待确认"} · ${claim.text}`;
    if (claim.status === "confirmed") {
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox"; checkbox.value = claim.id; checkbox.checked = selectedClaims.has(claim.id);
      row.append(checkbox);
    } else {
      const confirm = document.createElement("button");
      confirm.type = "button"; confirm.textContent = "确认真实";
      confirm.onclick = () => action("interview", async () => {
        await api(`/claims/${claim.id}/confirm`, { method: "POST" });
        await loadPreparation();
      });
      row.append(confirm);
    }
    row.append(text);
    const edit = document.createElement("button");
    edit.type = "button"; edit.className = "secondary"; edit.textContent = "编辑";
    edit.onclick = () => openResourceEditor("claim", claim);
    row.append(edit);
    const remove = document.createElement("button");
    remove.type = "button"; remove.className = "secondary"; remove.textContent = "撤回";
    remove.onclick = () => {
      if (!window.confirm("撤回这条经历？旧面试记录会保留。")) return;
      action("interview", async () => {
        await api(`/claims/${claim.id}`, { method: "DELETE" });
        await loadPreparation();
        notice("经历已撤回。");
      });
    };
    row.append(remove);
    claimBox.append(row);
  }
}

function renderSelectedResources(resources) {
  const box = $("selected-resources");
  const materialCount = resources?.materials.length || 0;
  const claimCount = resources?.claims.length || 0;
  box.hidden = !resources || (!materialCount && !claimCount);
  box.textContent = `本场已选择 ${materialCount} 份材料、${claimCount} 条已确认经历`;
}

function renderReport(report) {
  $("interview-report").hidden = !report;
  const box = $("report-items");
  const comparison = $("practice-compare");
  box.replaceChildren();
  comparison.replaceChildren();
  comparison.hidden = !report?.practice_source;
  $("discuss-report").hidden = !report?.items.some((item) => item.feedback);
  if (!report) return;
  $("report-summary").textContent = report.summary || "本轮尚未形成总结。";
  if (report.practice_source) {
    const source = report.practice_source;
    for (const value of ["复练对照：原题与原反馈", `原题：${source.question}`,
      `原回答：${source.answer || "无"}`, `原反馈：${source.feedback || "无"}`]) {
      const line = document.createElement("p");
      line.textContent = value;
      comparison.append(line);
    }
  }
  for (const item of report.items) {
    const row = document.createElement("div");
    row.className = "report-item";
    for (const [label, value] of [[`第 ${item.number} 题`, item.question], ["你的回答", item.skipped ? "已跳过" : item.answer], ["反馈", item.feedback]]) {
      const paragraph = document.createElement("p");
      paragraph.textContent = `${label}：${value || "无"}`;
      row.append(paragraph);
    }
    const evidence = document.createElement("p");
    evidence.textContent = `回答依据：${item.evidence?.map((quote) => `“${quote}”`).join("、") || "无"}`;
    row.append(evidence);
    if (item.feedback) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = "复练这题";
      button.disabled = !state.modelReady;
      button.onclick = () => startPractice(report.session_id, item.number);
      row.append(button);
    }
    box.append(row);
  }
}

function startPractice(sourceId, questionNumber) {
  if (!state.modelReady) return;
  action("interview", async () => {
    const session = await api(`/interview/sessions/${sourceId}/practice`, {
      method: "POST", body: JSON.stringify({ question_number: questionNumber }),
    });
    state.ids.interview = session.id;
    localStorage.setItem(sessionKey("interview"), session.id);
    try {
      const run = runRequest("interview", session.id, `/interview/sessions/${session.id}/start`);
      await selectSession(session.id);
      await run;
    }
    finally { if (state.kind === "interview" && state.ids.interview === session.id) await selectSession(session.id); }
  });
}

async function loadSessions() {
  const kind = state.kind;
  const query = state.sessionQueries[kind];
  const sequence = ++state.sessionSearchSeq[kind];
  const sessions = await api(`/${kind}/sessions${query ? `?q=${encodeURIComponent(query)}` : ""}`);
  if (sequence !== state.sessionSearchSeq[kind] || query !== state.sessionQueries[kind]) return;
  state.sessionLists[kind] = sessions;
  if (state.kind === kind) renderSessionList();
}

function renderSessionList() {
  const kind = state.kind;
  const sessions = state.sessionLists[kind];
  const query = state.sessionQueries[kind];
  $("session-count").textContent = query ? `找到 ${sessions.length} 个会话` : `${sessions.length} 个会话`;
  const box = $("sessions");
  box.replaceChildren();
  if (!sessions.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = query ? "没有匹配的会话，试试其他名称、消息内容或 ID。"
      : kind === "chat" ? "还没有聊天会话，发送消息后会显示在这里。" : "还没有面试会话，设置目标后开始一场练习。";
    box.append(empty);
  }
  for (const session of sessions) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = session.id === state.ids[kind] ? "selected" : "";
    if (session.id === state.ids[kind]) button.setAttribute("aria-current", "true");
    button.textContent = session.title || (kind === "chat" ? `聊天 ${session.id.slice(0, 6)}` : session.goal);
    const detail = document.createElement("small");
    detail.textContent = kind === "chat" ? new Date(session.created_at).toLocaleString() : `${session.question_count} 题 · ${session.status}`;
    button.append(detail);
    button.onclick = async () => {
      await selectSession(session.id);
      if (window.matchMedia("(max-width: 760px)").matches) {
        setHistoryOpen(false);
        $("workspace-title").scrollIntoView({ block: "start" });
      }
    };
    box.append(button);
  }
}

async function selectSession(id) {
  const kind = state.kind;
  const sequence = ++state.selectionSeq;
  state.ids[kind] = id;
  $("session-title-form").hidden = true;
  $("session-title-error").hidden = true;
  $("rename-session").hidden = !id;
  $("delete-session").hidden = !id;
  if (id) localStorage.setItem(sessionKey(kind), id);
  else localStorage.removeItem(sessionKey(kind));
  const view = id ? await api(`/${kind}/sessions/${id}`) : null;
  const current = () => sequence === state.selectionSeq && state.kind === kind && state.ids[kind] === id;
  if (!current()) return;
  $("workspace-title").textContent = view?.session.title || (kind === "chat" ? "新对话" : "AI 面试");
  $("workspace-subtitle").textContent = kind === "chat" ? "聊聊岗位、经历或下一步准备" : "每次聚焦一题，保留完整练习记录";
  if (kind === "chat") {
    $("chat-export").hidden = !view;
    if (view) $("chat-export").href = `/api/chat/sessions/${id}/export.md`;
    $("chat-notes").hidden = !view;
    if (!view) $("chat-notes").open = false;
    $("chat-memory").value = view?.session.chat_memory || "";
    $("chat-retry").hidden = !view || view.session.status !== "active" || view.messages.at(-1)?.role !== "user";
    $("chat-retry").disabled = !state.modelReady;
    $("chat-interrupt").hidden = !view || view.session.status !== "running";
  }
  renderMessages(kind, view?.messages || []);
  renderRunHistory(kind, view?.events || [], Boolean(view));
  if (kind === "interview") {
    renderInterviewState(view?.session);
    renderSelectedResources(view?.resources);
    const report = ["finished", "cancelled"].includes(view?.session.status) ? await api(`/interview/sessions/${id}/report`) : null;
    if (!current()) return;
    renderReport(report);
    if (!view) await loadPreparation();
  }
  if (!current()) return;
  await loadSessions();
  if (current()) notice();
}

async function switchKind(kind, fresh = false) {
  clearTimeout(sessionSearchTimer);
  state.kind = kind;
  const previousId = state.ids[kind];
  const id = fresh ? null : previousId;
  $("session-search").value = state.sessionQueries[kind];
  setHistoryOpen(false);
  $("history-heading").textContent = kind === "chat" ? "历史对话" : "历史面试";
  $("new-session").textContent = kind === "chat" ? "新建对话" : "新建面试";
  $("tab-chat").classList.toggle("active", kind === "chat");
  $("tab-interview").classList.toggle("active", kind === "interview");
  if (kind === "chat") { $("tab-chat").setAttribute("aria-current", "page"); $("tab-interview").removeAttribute("aria-current"); }
  else { $("tab-interview").setAttribute("aria-current", "page"); $("tab-chat").removeAttribute("aria-current"); }
  $("chat-panel").hidden = kind !== "chat";
  $("interview-panel").hidden = kind !== "interview";
  try { await selectSession(id); }
  catch (error) {
    if (!$("auth-panel").hidden || state.kind !== kind || state.ids[kind] !== id) return;
    state.ids[kind] = null;
    await selectSession(null);
    notice(previousId && error.message === "会话不存在" ? "上次的会话已不存在，已打开新建界面。" : error.message);
  }
  if (kind === "chat" && state.kind === kind && !$("shell").hidden) {
    try { await loadChatResources(); }
    catch (error) { notice(error.message); }
  }
}

async function action(kind, work) {
  if (state.busy[kind]) return;
  setBusy(kind, true); notice();
  try { await work(); } catch (error) { if (error.name !== "AbortError" && state.kind === kind) notice(error.message); } finally { setBusy(kind, false); }
}

async function runRequest(kind, id, path, body) {
  const controller = new AbortController();
  state.pending[kind] = { id, controller };
  try { return await api(path, { method: "POST", body: JSON.stringify(body), signal: controller.signal }); }
  finally { if (state.pending[kind]?.controller === controller) state.pending[kind] = null; }
}

async function performChatRun(id, path, body) {
  if (state.pending.chat?.id === id) state.pending.chat.controller.abort();
  const seq = ++state.chatSeq;
  let errorMessage = "";
  try {
    const run = runRequest("chat", id, path, body);
    if (state.kind === "chat" && state.ids.chat === id) await selectSession(id);
    notice("正在生成回复，可以继续发送补充消息。");
    await run;
  } catch (error) {
    if (error.name !== "AbortError") errorMessage = error.message;
  } finally {
    if (seq === state.chatSeq && state.kind === "chat" && state.ids.chat === id) {
      await selectSession(id);
      if (errorMessage) notice(errorMessage);
    }
  }
}

$("tab-chat").onclick = () => switchKind("chat", true);
$("tab-interview").onclick = () => switchKind("interview");
$("copy-interview-id").onclick = () => { if (state.ids.interview) copyId(state.ids.interview); };
$("discuss-report").onclick = async () => {
  const id = state.ids.interview;
  if (!id) return;
  await switchKind("chat");
  insertChatReference(id, "interview");
};
$("chat-resource").onchange = () => { $("chat-insert-resource").disabled = !$("chat-resource").value; };
$("chat-insert-resource").onclick = () => {
  const select = $("chat-resource");
  if (!select.value) return;
  insertChatReference(select.value, select.selectedOptions[0].dataset.kind);
  select.value = "";
  $("chat-insert-resource").disabled = true;
};
$("new-session").onclick = async () => {
  await selectSession(null);
  setHistoryOpen(false);
  (state.kind === "chat" ? $("chat-text") : $("interview-goal")).focus();
};
$("history-toggle").onclick = () => setHistoryOpen($("history-toggle").getAttribute("aria-expanded") !== "true");
$("session-search").oninput = () => {
  const kind = state.kind;
  const query = $("session-search").value.trim();
  state.sessionQueries[kind] = query;
  $("session-count").textContent = "正在查找…";
  clearTimeout(sessionSearchTimer);
  sessionSearchTimer = setTimeout(() => loadSessions().catch((error) => {
    if (state.kind === kind && state.sessionQueries[kind] === query) {
      $("session-count").textContent = "查找失败，请重试。";
      $("sessions").replaceChildren();
      notice(error.message);
    }
  }), 200);
};
$("rename-session").onclick = () => {
  const session = state.sessionLists[state.kind].find((item) => item.id === state.ids[state.kind]);
  if (!session) return;
  $("session-title").value = session.title;
  $("session-title-error").hidden = true;
  $("session-title-form").hidden = false;
  $("session-title").focus();
};
$("cancel-session-title").onclick = () => { $("session-title-form").hidden = true; };
$("session-title-form").onsubmit = async (event) => {
  event.preventDefault();
  const kind = state.kind;
  const id = state.ids[kind];
  if (!id) return;
  const title = $("session-title").value.trim();
  const submit = $("session-title-form").querySelector("button[type=submit]");
  submit.disabled = true;
  $("session-title-error").hidden = true;
  try {
    await api(`/${kind}/sessions/${id}/title`, { method: "PUT", body: JSON.stringify({ title }) });
    if (state.kind === kind && state.ids[kind] === id) {
      $("session-title-form").hidden = true;
      $("workspace-title").textContent = title || (kind === "chat" ? "新对话" : "AI 面试");
      await loadSessions();
      notice(title ? "会话名称已保存。" : "已恢复默认会话名称。");
    }
  } catch (error) {
    $("session-title-error").textContent = error.message;
    $("session-title-error").hidden = false;
  } finally { submit.disabled = false; }
};
$("chat-retry").onclick = async () => {
  const id = state.ids.chat;
  if (!id) return;
  $("chat-retry").disabled = true;
  try { await performChatRun(id, `/chat/sessions/${id}/retry`); }
  finally { $("chat-retry").disabled = !state.modelReady; }
};
$("chat-memory-form").onsubmit = (event) => {
  event.preventDefault();
  const id = state.ids.chat;
  if (!id) return;
  action("chat", async () => {
    await api(`/chat/sessions/${id}/memory`, {
      method: "PUT", body: JSON.stringify({ text: $("chat-memory").value }),
    });
    notice("当前聊天的备注已保存。");
  });
};
$("delete-session").onclick = async () => {
  const kind = state.kind;
  const id = state.ids[kind];
  if (!id || !window.confirm("删除当前会话及其记录？删除面试会同时删除由它创建的复练。")) return;
  try {
    await api(`/${kind}/sessions/${id}`, { method: "DELETE" });
    if (state.pending[kind]?.id === id) state.pending[kind].controller.abort();
    await selectSession(null);
    notice("会话已删除。");
  } catch (error) { notice(error.message); }
};
$("material-file").onchange = async (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    const body = new FormData();
    body.append("file", file);
    const extracted = await api("/materials/extract", { method: "POST", headers: {}, body });
    $("material-content").value = extracted.content;
    if (!$("material-title").value.trim()) $("material-title").value = file.name.replace(/\.(txt|md|docx|pdf)$/i, "");
    notice("文件内容已导入，请检查后保存材料。");
  } catch (error) { notice(error.message); }
  finally { event.target.value = ""; }
};
$("resource-edit-cancel").onclick = () => $("resource-editor").close();
$("resource-editor").onclose = () => { state.editing = null; };
$("resource-editor-form").onsubmit = async (event) => {
  event.preventDefault();
  if (!state.editing) return;
  const { kind, id } = state.editing;
  const content = $("resource-edit-content").value.trim();
  const title = $("resource-edit-title").value.trim();
  const errorBox = $("resource-edit-error");
  errorBox.hidden = true;
  if (!content || (kind === "material" && !title)) return;
  const submit = $("resource-editor-form").querySelector("button[type=submit]");
  submit.disabled = true;
  try {
    const path = kind === "material" ? `/materials/${id}` : `/claims/${id}`;
    const body = kind === "material" ? { title, content } : { text: content };
    await api(path, { method: "PUT", body: JSON.stringify(body) });
    $("resource-editor").close();
    await loadPreparation();
    notice(kind === "material" ? "材料新版本已保存；关联旧材料的经历已撤回。" : "经历新版本已保存为待确认草稿。");
  } catch (error) {
    if ($("resource-editor").open) {
      errorBox.textContent = error.message;
      errorBox.hidden = false;
    } else notice(error.message);
  } finally { submit.disabled = false; }
};
$("material-form").onsubmit = (event) => {
  event.preventDefault();
  action("interview", async () => {
    await api("/materials", { method: "POST", body: JSON.stringify({
      kind: $("material-kind").value,
      title: $("material-title").value.trim(),
      content: $("material-content").value.trim(),
    }) });
    $("material-title").value = "";
    $("material-content").value = "";
    await loadPreparation();
  });
};
$("claim-form").onsubmit = (event) => {
  event.preventDefault();
  action("interview", async () => {
    await api("/claims", { method: "POST", body: JSON.stringify({
      text: $("claim-text").value.trim(),
      material_id: $("claim-source").value || null,
    }) });
    $("claim-text").value = "";
    await loadPreparation();
  });
};
$("chat-form").onsubmit = async (event) => {
  event.preventDefault();
  if (!state.modelReady) return;
  const text = $("chat-text").value.trim();
  if (!text || state.busy.chat) return;
  let id = state.ids.chat;
  setBusy("chat", true);
  let claimed;
  try {
    if (!id) {
      id = (await api("/chat/sessions", { method: "POST" })).id;
      state.ids.chat = id;
      localStorage.setItem(sessionKey("chat"), id);
    }
    claimed = await api(`/chat/sessions/${id}/messages/claim`, { method: "POST", body: JSON.stringify({ text }) });
  } catch (error) { notice(error.message); return; }
  finally { setBusy("chat", false); }
  $("chat-text").value = "";
  await performChatRun(id, `/chat/sessions/${id}/messages/run`, { version: claimed.version });
};
$("chat-interrupt").onclick = async () => {
  if (!state.ids.chat) return;
  try {
    await api(`/chat/sessions/${state.ids.chat}/interrupt`, { method: "POST" });
    if (state.pending.chat?.id === state.ids.chat) state.pending.chat.controller.abort();
    await selectSession(state.ids.chat);
  }
  catch (error) { notice(error.message); }
};
$("interview-setup").onsubmit = (event) => {
  event.preventDefault();
  if (!state.modelReady) return;
  action("interview", async () => {
    const goal = $("interview-goal").value.trim();
    if (!goal) return;
    const max_questions = Number($("interview-max-questions").value);
    const material_ids = [...$("material-list").querySelectorAll("input:checked")].map((item) => item.value);
    const claim_ids = [...$("claim-list").querySelectorAll("input:checked")].map((item) => item.value);
    const session = await api("/interview/sessions", { method: "POST", body: JSON.stringify({ goal, max_questions, material_ids, claim_ids }) });
    state.ids.interview = session.id;
    localStorage.setItem(sessionKey("interview"), session.id);
    try {
      const run = runRequest("interview", session.id, `/interview/sessions/${session.id}/start`);
      await selectSession(session.id);
      await run;
    }
    finally { if (state.kind === "interview" && state.ids.interview === session.id) await selectSession(session.id); }
  });
};
$("answer-form").onsubmit = async (event) => {
  event.preventDefault();
  if (!state.modelReady) return;
  const text = $("answer-text").value.trim();
  if (!text || state.busy.interview) return;
  const id = state.ids.interview;
  const question_number = state.interviewQuestion;
  setBusy("interview", true);
  let claimed;
  try {
    claimed = await api(`/interview/sessions/${id}/answers/claim`, {
      method: "POST", body: JSON.stringify({ text, question_number }),
    });
  } catch (error) { notice(error.message); return; }
  finally { setBusy("interview", false); }
  $("answer-text").value = "";
  if (state.pending.interview?.id === id) state.pending.interview.controller.abort();
  const seq = ++state.interviewSeq;
  let errorMessage = "";
  try {
    const run = runRequest("interview", id, `/interview/sessions/${id}/answers/run`, { version: claimed.version });
    if (state.kind === "interview" && state.ids.interview === id) await selectSession(id);
    notice("正在评估本题；需要更正时可再次提交回答。");
    await run;
  } catch (error) {
    if (error.name !== "AbortError") errorMessage = error.message;
  } finally {
    if (seq === state.interviewSeq && state.kind === "interview" && state.ids.interview === id) {
      await selectSession(id);
      if (errorMessage) notice(errorMessage);
    }
  }
};
async function pauseInterview() {
  try {
    await api(`/interview/sessions/${state.ids.interview}/pause`, { method: "POST" });
    if (state.pending.interview?.id === state.ids.interview) state.pending.interview.controller.abort();
    await selectSession(state.ids.interview);
  }
  catch (error) { notice(error.message); }
}
$("pause-button").onclick = pauseInterview;
$("pause-running").onclick = pauseInterview;
$("skip-question").onclick = () => action("interview", async () => {
  const id = state.ids.interview;
  const question_number = state.interviewQuestion;
  try {
    const run = runRequest("interview", id, `/interview/sessions/${id}/skip`, { question_number });
    await selectSession(id);
    await run;
  } finally { if (state.kind === "interview" && state.ids.interview === id) await selectSession(id); }
});
$("cancel-interview").onclick = async () => {
  const id = state.ids.interview;
  if (!id || !window.confirm("结束本轮面试？已保存的回答和反馈会保留。")) return;
  try {
    await api(`/interview/sessions/${id}/cancel`, { method: "POST" });
    if (state.pending.interview?.id === id) state.pending.interview.controller.abort();
    await selectSession(id);
  } catch (error) { notice(error.message); }
};
$("resume-button").onclick = () => action("interview", async () => {
  const id = state.ids.interview;
  try { await runRequest("interview", id, `/interview/sessions/${id}/resume`); }
  finally { if (state.kind === "interview" && state.ids.interview === id) await selectSession(id); }
});

async function authenticate(mode) {
  const username = $("auth-username").value.trim();
  const password = $("auth-password").value;
  try {
    const user = await api(`/auth/${mode}`, { method: "POST", body: JSON.stringify({ username, password }) });
    $("auth-password").value = "";
    $("auth-notice").textContent = "";
    showApp(user.username);
    await switchKind("chat");
  } catch (error) { $("auth-notice").textContent = error.message; }
}
$("auth-form").onsubmit = (event) => { event.preventDefault(); authenticate("login"); };
$("auth-register").onclick = () => authenticate("register");
$("logout").onclick = async () => {
  try { await api("/auth/logout", { method: "POST" }); showAuth(); }
  catch (error) { notice(error.message); }
};

async function bootstrap() {
  try {
    const config = await api("/config");
    state.modelReady = !config.config_error;
    $("provider").textContent = config.config_error ? "模型未就绪" : config.provider === "demo" ? "演示模式" : `模型：${config.model}`;
    $("provider-data-notice").hidden = config.provider === "demo";
    $("model-config-notice").hidden = state.modelReady;
    $("model-config-notice").textContent = config.config_error ? `${config.config_error}；修改环境变量后重启应用。` : "";
    setBusy("chat", false);
    setBusy("interview", false);
    $("logout").hidden = !config.auth_required;
    if (config.auth_required) {
      try {
        const user = await api("/auth/me");
        showApp(user.username);
      } catch { showAuth(); return; }
    } else showApp();
    await switchKind("chat");
  } catch (error) {
    showAuth();
    $("auth-notice").textContent = error.message;
  }
}
bootstrap();
