const state = { kind: "chat", ids: { chat: localStorage.getItem("chatSession"), interview: localStorage.getItem("interviewSession") }, busy: { chat: false, interview: false }, pending: { chat: null, interview: null } };
const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, { headers: { "Content-Type": "application/json" }, ...options });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || `请求失败 (${response.status})`);
  return data;
}

function notice(message = "") { $("notice").textContent = message; }
function setBusy(kind, value) {
  state.busy[kind] = value;
  document.querySelectorAll(`#${kind === "chat" ? "chat-panel" : "interview-panel"} button[type=submit]`).forEach((button) => { button.disabled = value; });
}
function renderMessages(kind, messages) {
  const box = $(`${kind}-messages`);
  box.replaceChildren();
  if (!messages.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = kind === "chat" ? "发一条消息，开始你的求职讨论。" : "设置目标岗位后开始面试。";
    box.append(empty);
  }
  for (const message of messages) {
    const item = document.createElement("div");
    item.className = `message ${message.role}`;
    const role = document.createElement("span");
    role.className = "role";
    role.textContent = message.role === "user" ? "你" : kind === "chat" ? "聊天助手" : "面试官";
    item.append(role, document.createTextNode(message.content));
    box.append(item);
  }
  box.scrollTop = box.scrollHeight;
}

function renderInterviewState(session) {
  $("interview-setup").hidden = Boolean(session);
  $("answer-form").hidden = !session || session.status !== "awaiting_answer";
  $("resume-actions").hidden = !session || session.status !== "paused";
  const names = { new: "尚未开始", running: "正在生成回复", awaiting_answer: `等待回答 · 第 ${session?.question_count ?? 0} 题`, paused: "已暂停", finished: "已完成" };
  $("interview-status").textContent = session ? `${session.goal} · ${names[session.status]}` : "创建一场面试";
}

async function loadSessions() {
  const sessions = await api(`/${state.kind}/sessions`);
  const box = $("sessions");
  box.replaceChildren();
  if (!sessions.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "还没有会话";
    box.append(empty);
  }
  for (const session of sessions) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = session.id === state.ids[state.kind] ? "selected" : "";
    button.textContent = state.kind === "chat" ? `聊天 ${session.id.slice(0, 6)}` : session.goal;
    const detail = document.createElement("small");
    detail.textContent = state.kind === "chat" ? new Date(session.created_at).toLocaleString() : `${session.question_count} 题 · ${session.status}`;
    button.append(detail);
    button.onclick = () => selectSession(session.id);
    box.append(button);
  }
}

async function selectSession(id) {
  state.ids[state.kind] = id;
  if (id) localStorage.setItem(`${state.kind}Session`, id);
  else localStorage.removeItem(`${state.kind}Session`);
  const view = id ? await api(`/${state.kind}/sessions/${id}`) : null;
  renderMessages(state.kind, view?.messages || []);
  if (state.kind === "interview") renderInterviewState(view?.session);
  await loadSessions();
  notice();
}

async function switchKind(kind) {
  state.kind = kind;
  $("tab-chat").classList.toggle("active", kind === "chat");
  $("tab-interview").classList.toggle("active", kind === "interview");
  $("chat-panel").hidden = kind !== "chat";
  $("interview-panel").hidden = kind !== "interview";
  try { await selectSession(state.ids[kind]); } catch (error) { state.ids[kind] = null; await selectSession(null); notice(error.message); }
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

$("tab-chat").onclick = () => switchKind("chat");
$("tab-interview").onclick = () => switchKind("interview");
$("new-session").onclick = () => selectSession(null);
$("chat-form").onsubmit = (event) => {
  event.preventDefault();
  action("chat", async () => {
    const text = $("chat-text").value.trim();
    if (!text) return;
    let id = state.ids.chat;
    if (!id) { id = (await api("/chat/sessions", { method: "POST" })).id; state.ids.chat = id; localStorage.setItem("chatSession", id); }
    $("chat-text").value = "";
    try { await runRequest("chat", id, `/chat/sessions/${id}/messages`, { text }); }
    finally { if (state.kind === "chat" && state.ids.chat === id) await selectSession(id); }
  });
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
  action("interview", async () => {
    const goal = $("interview-goal").value.trim();
    if (!goal) return;
    const session = await api("/interview/sessions", { method: "POST", body: JSON.stringify({ goal }) });
    state.ids.interview = session.id;
    localStorage.setItem("interviewSession", session.id);
    try { await api(`/interview/sessions/${session.id}/start`, { method: "POST" }); }
    finally { if (state.kind === "interview" && state.ids.interview === session.id) await selectSession(session.id); }
  });
};
$("answer-form").onsubmit = (event) => {
  event.preventDefault();
  action("interview", async () => {
    const text = $("answer-text").value.trim();
    if (!text) return;
    $("answer-text").value = "";
    const id = state.ids.interview;
    try { await runRequest("interview", id, `/interview/sessions/${id}/answers`, { text }); }
    finally { if (state.kind === "interview" && state.ids.interview === id) await selectSession(id); }
  });
};
$("pause-button").onclick = async () => {
  try {
    await api(`/interview/sessions/${state.ids.interview}/pause`, { method: "POST" });
    if (state.pending.interview?.id === state.ids.interview) state.pending.interview.controller.abort();
    await selectSession(state.ids.interview);
  }
  catch (error) { notice(error.message); }
};
$("resume-button").onclick = () => action("interview", async () => {
  const id = state.ids.interview;
  try { await runRequest("interview", id, `/interview/sessions/${id}/resume`); }
  finally { if (state.kind === "interview" && state.ids.interview === id) await selectSession(id); }
});

api("/config").then((config) => { $("provider").textContent = config.provider === "demo" ? "演示模式" : `模型：${config.model}`; });
switchKind("chat");
