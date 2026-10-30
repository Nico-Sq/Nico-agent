const state = { kind: "chat", ids: { chat: localStorage.getItem("chatSession"), interview: localStorage.getItem("interviewSession") }, busy: { chat: false, interview: false }, pending: { chat: null, interview: null } };
const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, { headers: { "Content-Type": "application/json" }, ...options });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || `请求失败 (${response.status})`);
  return data;
}

function notice(message = "") { $("notice").textContent = message; }
async function copyId(id) {
  try { await navigator.clipboard.writeText(id); notice("ID 已复制"); }
  catch { notice(`ID：${id}`); }
}
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

function renderInterviewState(session) {
  $("copy-interview-id").hidden = !session;
  $("preparation").hidden = Boolean(session);
  $("interview-setup").hidden = Boolean(session);
  $("answer-form").hidden = !session || session.status !== "awaiting_answer";
  $("resume-actions").hidden = !session || session.status !== "paused";
  const names = { new: "尚未开始", running: "正在生成回复", awaiting_answer: `等待回答 · 第 ${session?.question_count ?? 0} 题`, paused: "已暂停", finished: "已完成" };
  $("interview-status").textContent = session ? `${session.goal} · ${names[session.status]}` : "创建一场面试";
}

async function loadPreparation() {
  const [materials, claims] = await Promise.all([api("/materials"), api("/claims")]);
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
    edit.onclick = () => {
      const title = window.prompt("编辑材料标题；旧面试仍保留原版本", material.title);
      if (!title?.trim()) return;
      const content = window.prompt("编辑材料内容", material.content);
      if (!content?.trim()) return;
      action("interview", async () => {
        await api(`/materials/${material.id}`, { method: "PUT", body: JSON.stringify({ title: title.trim(), content: content.trim() }) });
        await loadPreparation();
        notice("新版本已保存；关联旧材料的经历已撤回，请重新确认。");
      });
    };
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
    edit.onclick = () => {
      const value = window.prompt("编辑经历；新版本需要重新确认真实", claim.text);
      if (!value?.trim()) return;
      action("interview", async () => {
        await api(`/claims/${claim.id}`, { method: "PUT", body: JSON.stringify({ text: value.trim() }) });
        await loadPreparation();
        notice("经历新版本已保存为待确认草稿。");
      });
    };
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
  box.replaceChildren();
  if (!report) return;
  $("report-summary").textContent = report.summary || "本轮尚未形成总结。";
  for (const item of report.items) {
    const row = document.createElement("div");
    row.className = "report-item";
    for (const [label, value] of [[`第 ${item.number} 题`, item.question], ["你的回答", item.answer], ["反馈", item.feedback]]) {
      const paragraph = document.createElement("p");
      paragraph.textContent = `${label}：${value || "无"}`;
      row.append(paragraph);
    }
    const evidence = document.createElement("p");
    evidence.textContent = `回答依据：${item.evidence?.map((quote) => `“${quote}”`).join("、") || "无"}`;
    row.append(evidence);
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "复练这题";
    button.onclick = () => startPractice(report.session_id, item.number);
    row.append(button);
    box.append(row);
  }
}

function startPractice(sourceId, questionNumber) {
  action("interview", async () => {
    const session = await api(`/interview/sessions/${sourceId}/practice`, {
      method: "POST", body: JSON.stringify({ question_number: questionNumber }),
    });
    state.ids.interview = session.id;
    localStorage.setItem("interviewSession", session.id);
    try { await runRequest("interview", session.id, `/interview/sessions/${session.id}/start`); }
    finally { if (state.kind === "interview" && state.ids.interview === session.id) await selectSession(session.id); }
  });
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
  if (state.kind === "interview") {
    renderInterviewState(view?.session);
    renderSelectedResources(view?.resources);
    renderReport(view?.session.status === "finished" ? await api(`/interview/sessions/${id}/report`) : null);
    if (!view) await loadPreparation();
  }
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
$("copy-interview-id").onclick = () => { if (state.ids.interview) copyId(state.ids.interview); };
$("new-session").onclick = () => selectSession(null);
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
    const material_ids = [...$("material-list").querySelectorAll("input:checked")].map((item) => item.value);
    const claim_ids = [...$("claim-list").querySelectorAll("input:checked")].map((item) => item.value);
    const session = await api("/interview/sessions", { method: "POST", body: JSON.stringify({ goal, material_ids, claim_ids }) });
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
