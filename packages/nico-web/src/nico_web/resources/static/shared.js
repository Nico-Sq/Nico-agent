const state = { kind: "chat", username: null, modelReady: true, models: [], ids: { chat: null, interview: null }, busy: { chat: false, interview: false }, pending: { chat: null, interview: null }, sessionLists: { chat: [], interview: [] }, sessionQueries: { chat: "", interview: "" }, sessionSearchSeq: { chat: 0, interview: 0 }, selectionSeq: 0, chatSeq: 0, interviewSeq: 0, interviewQuestion: 0, editing: null };
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
  state.models = [];
  for (const id of ["chat-form", "answer-form", "material-form", "claim-form", "interview-setup", "chat-memory-form"]) $(id).reset();
  if ($("resource-editor").open) $("resource-editor").close();
  if ($("platform-settings").open) $("platform-settings").close();
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
function updateModelReadiness() {
  const selected = $(state.kind === "chat" ? "chat-model" : "interview-model").value;
  const model = state.models.find((item) => item.id === selected);
  state.modelReady = Boolean(model && !model.config_error);
  $("provider").textContent = model ? model.provider === "demo" ? "演示模式" : `模型：${model.model}` : "模型不可用";
  $("provider-data-notice").hidden = !model || model.provider === "demo";
  $("model-config-notice").hidden = state.modelReady;
  $("model-config-notice").textContent = state.modelReady ? "" : model?.config_error || "所选模型已移除，请创建新会话并选择可用模型。";
  setBusy("chat", state.busy.chat);
  setBusy("interview", state.busy.interview);
}
async function loadModels() {
  state.models = await api("/models");
  for (const id of ["chat-model", "interview-model"]) {
    const select = $(id);
    select.replaceChildren();
    for (const model of state.models) {
      const option = new Option(`${model.id} · ${model.provider === "demo" ? "演示模式" : model.model}${model.config_error ? "（未就绪）" : ""}`, model.id);
      select.append(option);
    }
    select.value = "default";
  }
  updateModelReadiness();
}
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
  const modelSelect = $(kind === "chat" ? "chat-model" : "interview-model");
  if (view && !state.models.some((item) => item.id === view.session.model_id)) {
    modelSelect.append(new Option(`${view.session.model_id}（已移除）`, view.session.model_id));
  }
  modelSelect.value = view?.session.model_id || "default";
  modelSelect.disabled = Boolean(view);
  updateModelReadiness();
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
