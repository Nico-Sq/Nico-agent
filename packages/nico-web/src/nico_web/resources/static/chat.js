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
      if ($("platform-settings").open) await loadMemoryCandidateSettings();
    }
  }
}

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
      id = (await api("/chat/sessions", { method: "POST", body: JSON.stringify({ model_id: $("chat-model").value }) })).id;
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
