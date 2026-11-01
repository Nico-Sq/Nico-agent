function settingsNotice(message = "") { $("settings-notice").textContent = message; }
function renderModelSettings() {
  const box = $("settings-model-list");
  box.replaceChildren();
  for (const model of nicoState.models) {
    const row = document.createElement("div");
    row.className = "settings-row";
    const content = document.createElement("div");
    content.className = "settings-row-content";
    const title = document.createElement("strong");
    title.textContent = `${model.id} · ${model.provider}`;
    const detail = document.createElement("small");
    detail.textContent = `${model.model || "演示模型"} · 输出上限 ${model.max_output_tokens ?? "—"} · ${model.config_error || "已就绪，支持工具调用"}`;
    content.append(title, detail);
    row.append(content);
    box.append(row);
  }
}
async function loadToolSettings() {
  const box = $("settings-tool-list");
  box.replaceChildren();
  const id = nicoState.ids[nicoState.kind];
  if (!id) {
    box.textContent = "先创建或打开一个会话，再授权外部工具。";
    $("save-tool-grants").disabled = true;
    return;
  }
  const kind = nicoState.kind;
  try {
    const [tools, grants] = await Promise.all([api("/tools"), api(`/${kind}/sessions/${id}/tools`)]);
    if (nicoState.ids[nicoState.kind] !== id || nicoState.kind !== kind) return;
    if (!tools.length) box.textContent = "当前没有配置外部工具。";
    for (const [index, tool] of tools.entries()) {
      const readOnly = tool.read_only === true;
      const label = document.createElement("label");
      label.className = "settings-row";
      if (!readOnly) label.classList.add("is-disabled");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = tool.name;
      checkbox.checked = readOnly && grants.names.includes(tool.name);
      checkbox.disabled = !readOnly;
      checkbox.setAttribute("aria-describedby", `settings-tool-status-${index}`);
      const content = document.createElement("span");
      content.className = "settings-row-content";
      const name = document.createElement("strong");
      name.textContent = tool.name;
      const description = document.createElement("small");
      description.id = `settings-tool-status-${index}`;
      description.textContent = `${tool.description || "无描述"} · ${readOnly ? "只读，可授权" : "非只读，当前不可授权"}`;
      content.append(name, description);
      label.append(checkbox, content);
      box.append(label);
    }
    $("save-tool-grants").disabled = !tools.length;
  } catch (error) {
    box.textContent = "工具发现失败，请检查本地服务器配置。";
    $("save-tool-grants").disabled = true;
    settingsNotice(error.message);
  }
}

let memoryCandidateLoadSeq = 0;
function setCandidateBusy(form, busy) {
  form.querySelectorAll("button, textarea").forEach((control) => { control.disabled = busy; });
  form.setAttribute("aria-busy", String(busy));
}

async function loadMemoryCandidateSettings() {
  const box = $("settings-memory-candidate-list");
  const sequence = ++memoryCandidateLoadSeq;
  const kind = nicoState.kind;
  const chatId = nicoState.ids.chat;
  box.replaceChildren();
  if (kind !== "chat") {
    box.textContent = "切换到“新对话”后查看该会话的待确认记忆。";
    return;
  }
  if (!chatId) {
    box.textContent = "发送第一条聊天消息后，可在这里核对候选记忆。";
    return;
  }
  const loading = document.createElement("p");
  loading.className = "empty";
  loading.textContent = "正在读取待确认记忆…";
  box.append(loading);
  try {
    const candidates = await api(`/memory-candidates?session_id=${encodeURIComponent(chatId)}&status=pending`);
    if (sequence !== memoryCandidateLoadSeq || nicoState.kind !== kind || nicoState.ids.chat !== chatId) return;
    box.replaceChildren();
    if (!candidates.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "当前聊天没有待确认记忆。请助手记住某个信息，或稍后再来核对。";
      box.append(empty);
      return;
    }
    for (const candidate of candidates) {
      const form = document.createElement("form");
      form.className = "candidate-card";
      form.dataset.candidateId = candidate.id;
      const label = document.createElement("label");
      label.htmlFor = `candidate-content-${candidate.id}`;
      label.textContent = "候选内容";
      const editor = document.createElement("textarea");
      editor.id = `candidate-content-${candidate.id}`;
      editor.rows = 3;
      editor.maxLength = 2000;
      editor.required = true;
      editor.value = candidate.content;
      const sourceDetails = document.createElement("details");
      sourceDetails.className = "candidate-sources";
      const sourceSummary = document.createElement("summary");
      sourceSummary.textContent = `查看来源消息（${candidate.sources.length} 条）`;
      sourceDetails.append(sourceSummary);
      for (const source of candidate.sources) {
        const sourceRow = document.createElement("div");
        sourceRow.className = "candidate-source";
        const meta = document.createElement("small");
        meta.textContent = `${source.role === "user" ? "用户" : "助手"} · 消息 ${source.id} · ${new Date(source.created_at).toLocaleString()}`;
        const content = document.createElement("p");
        content.textContent = source.content;
        sourceRow.append(meta, content);
        sourceDetails.append(sourceRow);
      }
      const errorBox = document.createElement("p");
      errorBox.className = "candidate-error";
      errorBox.setAttribute("role", "alert");
      errorBox.hidden = true;
      const actions = document.createElement("div");
      actions.className = "candidate-actions";
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "secondary danger";
      remove.textContent = "删除候选";
      const save = document.createElement("button");
      save.type = "button";
      save.className = "secondary";
      save.textContent = "保存修改";
      const confirm = document.createElement("button");
      confirm.type = "submit";
      confirm.textContent = "确认写入长期记忆";
      actions.append(remove, save, confirm);
      form.append(label, editor, sourceDetails, errorBox, actions);

      const showError = (message) => {
        errorBox.textContent = message;
        errorBox.hidden = false;
      };
      const currentContent = () => editor.value.trim();
      save.onclick = async () => {
        if (!currentContent()) { showError("候选内容不能为空。"); editor.focus(); return; }
        errorBox.hidden = true;
        setCandidateBusy(form, true);
        try {
          await api(`/memory-candidates/${candidate.id}`, {
            method: "PUT", body: JSON.stringify({ content: currentContent() }),
          });
          settingsNotice("候选内容已保存，仍需确认后才会成为长期记忆。");
          await loadMemoryCandidateSettings();
        } catch (error) {
          setCandidateBusy(form, false);
          showError(error.message);
        }
      };
      remove.onclick = async () => {
        if (!window.confirm("删除这条待确认记忆？原始聊天消息不会删除。")) return;
        errorBox.hidden = true;
        setCandidateBusy(form, true);
        try {
          await api(`/memory-candidates/${candidate.id}`, { method: "DELETE" });
          settingsNotice("候选记忆已删除。");
          await loadMemoryCandidateSettings();
        } catch (error) {
          setCandidateBusy(form, false);
          showError(error.message);
        }
      };
      form.onsubmit = async (event) => {
        event.preventDefault();
        if (!currentContent()) { showError("候选内容不能为空。"); editor.focus(); return; }
        errorBox.hidden = true;
        setCandidateBusy(form, true);
        try {
          if (currentContent() !== candidate.content) {
            await api(`/memory-candidates/${candidate.id}`, {
              method: "PUT", body: JSON.stringify({ content: currentContent() }),
            });
          }
          await api(`/memory-candidates/${candidate.id}/confirm`, { method: "POST" });
          settingsNotice("候选已确认并写入长期记忆。");
          await Promise.all([loadMemoryCandidateSettings(), loadMemorySettings()]);
        } catch (error) {
          setCandidateBusy(form, false);
          showError(error.message);
        }
      };
      box.append(form);
    }
  } catch (error) {
    if (sequence !== memoryCandidateLoadSeq || nicoState.kind !== kind || nicoState.ids.chat !== chatId) return;
    box.replaceChildren();
    const failed = document.createElement("p");
    failed.className = "empty";
    failed.textContent = "候选记忆读取失败。";
    box.append(failed);
    settingsNotice(error.message);
  }
}

async function loadMemorySettings() {
  const box = $("settings-memory-list");
  const query = $("memory-search").value.trim();
  const items = await api(`/memories?q=${encodeURIComponent(query)}`);
  if (query !== $("memory-search").value.trim()) return;
  box.replaceChildren();
  if (!items.length) box.textContent = "没有匹配的记忆。";
  for (const item of items) {
    const row = document.createElement("div");
    row.className = "settings-row";
    const content = document.createElement("span");
    content.className = "settings-row-content";
    content.textContent = item.content;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary";
    button.textContent = "删除";
    button.setAttribute("aria-label", `删除记忆：${item.content.slice(0, 30)}`);
    button.onclick = async () => {
      if (!window.confirm("删除这条长期记忆？")) return;
      try { await api(`/memories/${item.id}`, { method: "DELETE" }); await loadMemorySettings(); settingsNotice("记忆已删除。") }
      catch (error) { settingsNotice(error.message); }
    };
    row.append(content, button);
    box.append(row);
  }
}
$("chat-model").onchange = updateModelReadiness;
$("interview-model").onchange = updateModelReadiness;
$("open-settings").onclick = async () => {
  $("platform-settings").showModal();
  settingsNotice();
  renderModelSettings();
  await Promise.allSettled([loadToolSettings(), loadMemoryCandidateSettings(), loadMemorySettings()]);
};
$("close-settings").onclick = () => $("platform-settings").close();
$("save-tool-grants").onclick = async () => {
  const id = nicoState.ids[nicoState.kind];
  if (!id) return;
  const names = [...$("settings-tool-list").querySelectorAll("input:checked")].map((item) => item.value);
  $("save-tool-grants").disabled = true;
  try {
    await api(`/${nicoState.kind}/sessions/${id}/tools`, { method: "PUT", body: JSON.stringify({ names }) });
    settingsNotice("工具授权已保存，将在下次运行时生效。");
  } catch (error) { settingsNotice(error.message); }
  finally { $("save-tool-grants").disabled = false; }
};
$("long-memory-form").onsubmit = async (event) => {
  event.preventDefault();
  const content = $("long-memory-input").value.trim();
  if (!content) return;
  try {
    await api("/memories", { method: "POST", body: JSON.stringify({ content }) });
    $("long-memory-input").value = "";
    await loadMemorySettings();
    settingsNotice("记忆已保存。")
  } catch (error) { settingsNotice(error.message); }
};
let memorySearchTimer;
$("memory-search").oninput = () => {
  clearTimeout(memorySearchTimer);
  memorySearchTimer = setTimeout(() => loadMemorySettings().catch((error) => settingsNotice(error.message)), 200);
};

async function authenticate(mode) {
  const username = $("auth-username").value.trim();
  const password = $("auth-password").value;
  try {
    const user = await api(`/auth/${mode}`, { method: "POST", body: JSON.stringify({ username, password }) });
    $("auth-password").value = "";
    $("auth-notice").textContent = "";
    showApp(user.username);
    await loadModels();
    await switchKind("chat");
  } catch (error) { $("auth-notice").textContent = error.message; }
}
$("auth-form").onsubmit = (event) => { event.preventDefault(); authenticate("login"); };
$("auth-register").onclick = () => authenticate("register");
$("logout").onclick = async () => {
  try { await api("/auth/logout", { method: "POST" }); showAuth(); }
  catch (error) { notice(error.message); }
};
