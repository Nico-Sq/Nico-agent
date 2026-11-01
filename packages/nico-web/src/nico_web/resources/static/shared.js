const nicoState = { kind: "chat", username: null, modelReady: true, models: [], ids: { chat: null, interview: null }, busy: { chat: false, interview: false }, pending: { chat: null, interview: null }, sessionLists: { chat: [], interview: [] }, sessionQueries: { chat: "", interview: "" }, sessionSearchSeq: { chat: 0, interview: 0 }, selectionSeq: 0, chatSeq: 0, interviewSeq: 0, interviewQuestion: 0, editing: null, careerResources: { materials: [], sessions: [] } };
const $ = (id) => document.getElementById(id);
const sessionKey = (kind) => `${kind}Session${nicoState.username ? `:${nicoState.username}` : ""}`;
let sessionSearchTimer;

function showAuth() {
  clearTimeout(sessionSearchTimer);
  nicoState.selectionSeq++;
  $("shell").hidden = true;
  $("auth-panel").hidden = false;
  for (const kind of ["chat", "interview"]) nicoState.pending[kind]?.controller.abort();
  nicoState.pending = { chat: null, interview: null };
  nicoState.chatSeq++;
  nicoState.interviewSeq++;
  setBusy("chat", false);
  setBusy("interview", false);
  nicoState.ids = { chat: null, interview: null };
  nicoState.sessionLists = { chat: [], interview: [] };
  nicoState.sessionQueries = { chat: "", interview: "" };
  nicoState.sessionSearchSeq.chat++;
  nicoState.sessionSearchSeq.interview++;
  nicoState.username = null;
  nicoState.models = [];
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
  $("workspace-title").textContent = "求职工作台";
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
  nicoState.username = username;
  nicoState.ids = { chat: localStorage.getItem(sessionKey("chat")), interview: localStorage.getItem(sessionKey("interview")) };
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
  const selected = $(nicoState.kind === "chat" ? "chat-model" : "interview-model").value;
  const model = nicoState.models.find((item) => item.id === selected);
  nicoState.modelReady = Boolean(model && !model.config_error);
  $("provider").textContent = model ? model.provider === "demo" ? "演示模式" : `模型：${model.model}` : "模型不可用";
  $("provider-data-notice").hidden = !model || model.provider === "demo";
  $("model-config-notice").hidden = nicoState.modelReady;
  $("model-config-notice").textContent = nicoState.modelReady ? "" : model?.config_error || "所选模型已移除，请创建新会话并选择可用模型。";
  setBusy("chat", nicoState.busy.chat);
  setBusy("interview", nicoState.busy.interview);
}
async function loadModels() {
  nicoState.models = await api("/models");
  for (const id of ["chat-model", "interview-model"]) {
    const select = $(id);
    select.replaceChildren();
    for (const model of nicoState.models) {
      const option = new Option(`${model.id} · ${model.provider === "demo" ? "演示模式" : model.model}${model.config_error ? "（未就绪）" : ""}`, model.id);
      select.append(option);
    }
    select.value = "default";
  }
  updateModelReadiness();
}
function setHistoryOpen(open) {
  $("sidebar-history").classList.toggle("open", open);
  document.querySelector(".sidebar").classList.toggle("mobile-open", open);
  $("history-toggle").setAttribute("aria-expanded", String(open));
  $("history-toggle").textContent = `${open ? "收起" : "展开"}${nicoState.kind === "chat" ? "历史对话" : "历史面试"}`;
}
async function copyId(id) {
  try { await navigator.clipboard.writeText(id); notice("ID 已复制"); }
  catch { notice(`ID：${id}`); }
}
function setBusy(kind, value) {
  nicoState.busy[kind] = value;
  document.querySelectorAll(`#${kind === "chat" ? "chat-panel" : "interview-panel"} button[type=submit]`).forEach((button) => {
    button.disabled = value || (!nicoState.modelReady && Boolean(button.closest("#chat-form, #interview-setup, #answer-form")));
  });
}
async function loadSessions() {
  const kind = nicoState.kind;
  const query = nicoState.sessionQueries[kind];
  const sequence = ++nicoState.sessionSearchSeq[kind];
  const sessions = await api(`/${kind}/sessions${query ? `?q=${encodeURIComponent(query)}` : ""}`);
  if (sequence !== nicoState.sessionSearchSeq[kind] || query !== nicoState.sessionQueries[kind]) return;
  nicoState.sessionLists[kind] = sessions;
  if (nicoState.kind === kind) renderSessionList();
}

function renderSessionList() {
  const kind = nicoState.kind;
  const sessions = nicoState.sessionLists[kind];
  const query = nicoState.sessionQueries[kind];
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
    button.className = session.id === nicoState.ids[kind] ? "selected" : "";
    if (session.id === nicoState.ids[kind]) button.setAttribute("aria-current", "true");
    button.textContent = session.title || (kind === "chat" ? `聊天 ${session.id.slice(0, 6)}` : session.goal);
    const detail = document.createElement("small");
    detail.textContent = kind === "chat" ? new Date(session.created_at).toLocaleString() : `${session.question_count} 题 · ${session.status}`;
    button.append(detail);
    button.onclick = async () => {
      await selectSession(session.id);
      if (window.matchMedia("(max-width: 960px)").matches) {
        setHistoryOpen(false);
        $("workspace-title").scrollIntoView({ block: "start" });
      }
    };
    box.append(button);
  }
}

async function selectSession(id) {
  const kind = nicoState.kind;
  const sequence = ++nicoState.selectionSeq;
  nicoState.ids[kind] = id;
  $("session-title-form").hidden = true;
  $("session-title-error").hidden = true;
  $("rename-session").hidden = !id;
  $("delete-session").hidden = !id;
  if (id) localStorage.setItem(sessionKey(kind), id);
  else localStorage.removeItem(sessionKey(kind));
  const view = id ? await api(`/${kind}/sessions/${id}`) : null;
  const current = () => sequence === nicoState.selectionSeq && nicoState.kind === kind && nicoState.ids[kind] === id;
  if (!current()) return;
  $("workspace-title").textContent = view?.session.title || (kind === "chat" ? "求职工作台" : "AI 面试");
  $("workspace-subtitle").textContent = kind === "chat" ? "岗位 → 经历 → 面试 → 复盘" : "每次聚焦一题，保留完整练习记录";
  const modelSelect = $(kind === "chat" ? "chat-model" : "interview-model");
  if (view && !nicoState.models.some((item) => item.id === view.session.model_id)) {
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
    $("chat-retry").disabled = !nicoState.modelReady;
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
  nicoState.kind = kind;
  const previousId = nicoState.ids[kind];
  const id = fresh ? null : previousId;
  $("session-search").value = nicoState.sessionQueries[kind];
  setHistoryOpen(false);
  $("history-heading").textContent = kind === "chat" ? "历史对话" : "历史面试";
  $("new-session").querySelector("span").textContent = kind === "chat" ? "新建对话" : "新建面试";
  $("tab-chat").classList.toggle("active", kind === "chat");
  $("tab-interview").classList.toggle("active", kind === "interview");
  if (kind === "chat") { $("tab-chat").setAttribute("aria-current", "page"); $("tab-interview").removeAttribute("aria-current"); }
  else { $("tab-interview").setAttribute("aria-current", "page"); $("tab-chat").removeAttribute("aria-current"); }
  $("chat-panel").hidden = kind !== "chat";
  $("interview-panel").hidden = kind !== "interview";
  try { await selectSession(id); }
  catch (error) {
    if (!$("auth-panel").hidden || nicoState.kind !== kind || nicoState.ids[kind] !== id) return;
    nicoState.ids[kind] = null;
    await selectSession(null);
    notice(previousId && error.message === "会话不存在" ? "上次的会话已不存在，已打开新建界面。" : error.message);
  }
  if (kind === "chat" && nicoState.kind === kind && !$("shell").hidden) {
    try { await loadChatResources(); }
    catch (error) { notice(error.message); }
  }
}

async function action(kind, work) {
  if (nicoState.busy[kind]) return;
  setBusy(kind, true); notice();
  try { await work(); } catch (error) { if (error.name !== "AbortError" && nicoState.kind === kind) notice(error.message); } finally { setBusy(kind, false); }
}

function beginStreamMessage(kind) {
  const box = $(kind + "-messages");
  box.querySelector(".chat-empty, .empty")?.remove();
  const item = document.createElement("div");
  item.className = "message assistant";
  item.setAttribute("aria-live", "off");
  const role = document.createElement("span");
  role.className = "role";
  role.textContent = kind === "chat" ? "聊天助手" : "面试官";
  const status = document.createElement("span");
  status.textContent = "正在生成回复…";
  const content = document.createElement("span");
  item.append(role, status, content);
  box.append(item);
  box.scrollTop = box.scrollHeight;
  return { box, status, content };
}

async function readStream(response, bubble) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let completed = false;
  const handle = (block) => {
    const data = block.split("\n").filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart()).join("\n");
    if (!data) return;
    let event;
    try { event = JSON.parse(data); }
    catch { throw new Error("模型流返回了无效事件"); }
    if (event.type === "text_delta" && typeof event.text === "string") {
      bubble.status.hidden = true;
      bubble.content.textContent += event.text;
      bubble.box.scrollTop = bubble.box.scrollHeight;
    } else if (event.type === "tool_start" && typeof event.name === "string") {
      bubble.status.hidden = false;
      bubble.status.textContent = "正在使用工具：" + event.name;
      notice("正在使用工具：" + event.name);
    } else if (event.type === "error") {
      throw new Error(typeof event.detail === "string" ? event.detail : "模型运行失败，请稍后重试");
    } else if (event.type === "done") {
      completed = true;
    }
  };
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done }).replace(/\r\n/g, "\n").replace(/\r/g, "\n");
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        handle(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
      }
      if (done) break;
    }
    if (buffer.trim()) handle(buffer);
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally { reader.releaseLock(); }
  if (!completed) throw new Error("流式连接提前结束；请刷新会话查看运行状态。");
}

async function runRequest(kind, id, path, body) {
  const controller = new AbortController();
  nicoState.pending[kind] = { id, controller };
  const bubble = beginStreamMessage(kind);
  try {
    const response = await fetch("/api" + path, {
      method: "POST", headers: { "Content-Type": "application/json", Accept: "text/event-stream, application/json" },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: controller.signal,
    });
    const contentType = response.headers.get("Content-Type") || "";
    if (response.status === 401 && !path.startsWith("/auth/")) showAuth();
    if (!response.ok || !contentType.toLowerCase().includes("text/event-stream")) {
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        const detail = payload?.detail;
        const message = typeof detail === "string" ? detail
          : Array.isArray(detail) ? "输入不符合要求，请检查必填项、长度和格式。" : "请求失败 (" + response.status + ")";
        throw new Error(message);
      }
      return payload;
    }
    await readStream(response, bubble);
    return true;
  }
  finally { if (nicoState.pending[kind]?.controller === controller) nicoState.pending[kind] = null; }
}

$("tab-chat").onclick = () => switchKind("chat", true);
$("tab-interview").onclick = () => switchKind("interview");
$("copy-interview-id").onclick = () => { if (nicoState.ids.interview) copyId(nicoState.ids.interview); };
$("discuss-report").onclick = async () => {
  const id = nicoState.ids.interview;
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
  if (nicoState.kind === "chat") {
    try { await loadChatResources(); }
    catch (error) { notice(error.message); }
  }
  setHistoryOpen(false);
  (nicoState.kind === "chat" ? $("chat-text") : $("interview-goal")).focus();
};
$("history-toggle").onclick = () => setHistoryOpen($("history-toggle").getAttribute("aria-expanded") !== "true");
$("session-search").oninput = () => {
  const kind = nicoState.kind;
  const query = $("session-search").value.trim();
  nicoState.sessionQueries[kind] = query;
  $("session-count").textContent = "正在查找…";
  clearTimeout(sessionSearchTimer);
  sessionSearchTimer = setTimeout(() => loadSessions().catch((error) => {
    if (nicoState.kind === kind && nicoState.sessionQueries[kind] === query) {
      $("session-count").textContent = "查找失败，请重试。";
      $("sessions").replaceChildren();
      notice(error.message);
    }
  }), 200);
};
$("rename-session").onclick = () => {
  const session = nicoState.sessionLists[nicoState.kind].find((item) => item.id === nicoState.ids[nicoState.kind]);
  if (!session) return;
  $("session-title").value = session.title;
  $("session-title-error").hidden = true;
  $("session-title-form").hidden = false;
  $("session-title").focus();
};
$("cancel-session-title").onclick = () => { $("session-title-form").hidden = true; };
$("session-title-form").onsubmit = async (event) => {
  event.preventDefault();
  const kind = nicoState.kind;
  const id = nicoState.ids[kind];
  if (!id) return;
  const title = $("session-title").value.trim();
  const submit = $("session-title-form").querySelector("button[type=submit]");
  submit.disabled = true;
  $("session-title-error").hidden = true;
  try {
    await api(`/${kind}/sessions/${id}/title`, { method: "PUT", body: JSON.stringify({ title }) });
    if (nicoState.kind === kind && nicoState.ids[kind] === id) {
      $("session-title-form").hidden = true;
      $("workspace-title").textContent = title || (kind === "chat" ? "求职工作台" : "AI 面试");
      await loadSessions();
      notice(title ? "会话名称已保存。" : "已恢复默认会话名称。");
    }
  } catch (error) {
    $("session-title-error").textContent = error.message;
    $("session-title-error").hidden = false;
  } finally { submit.disabled = false; }
};
$("delete-session").onclick = async () => {
  const kind = nicoState.kind;
  const id = nicoState.ids[kind];
  if (!id || !window.confirm("删除当前会话及其记录？删除面试会同时删除由它创建的复练。")) return;
  try {
    await api(`/${kind}/sessions/${id}`, { method: "DELETE" });
    if (nicoState.pending[kind]?.id === id) nicoState.pending[kind].controller.abort();
    await selectSession(null);
    notice("会话已删除。");
  } catch (error) { notice(error.message); }
};

function applyTheme(theme) {
  const dark = theme === "dark";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  const toggle = $("theme-toggle");
  toggle.setAttribute("aria-label", dark ? "切换浅色主题" : "切换深色主题");
  toggle.setAttribute("title", dark ? "切换浅色主题" : "切换深色主题");
}

const savedTheme = localStorage.getItem("nicoTheme");
const systemPrefersDark = window.matchMedia("(prefers-color-scheme: dark)");
applyTheme(savedTheme || (systemPrefersDark.matches ? "dark" : "light"));
$("theme-toggle").onclick = () => {
  const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  localStorage.setItem("nicoTheme", theme);
  applyTheme(theme);
};
systemPrefersDark.addEventListener?.("change", (event) => {
  if (!localStorage.getItem("nicoTheme")) applyTheme(event.matches ? "dark" : "light");
});
