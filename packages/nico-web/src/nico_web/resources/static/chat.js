async function loadChatResources() {
  const sequence = nicoState.selectionSeq;
  const [materials, claims, sessions] = await Promise.all([
    api("/materials"),
    api("/claims"),
    api("/interview/sessions"),
  ]);
  if (sequence !== nicoState.selectionSeq || nicoState.kind !== "chat") return;
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
  const confirmedClaims = claims.filter((item) => item.status === "confirmed");
  nicoState.careerResources = { claims, materials, sessions };
  const inventory = document.querySelector(".career-inventory");
  if (inventory) {
    const feedbackCount = sessions.filter((item) => item.has_feedback).length;
    inventory.textContent = materials.length || confirmedClaims.length || sessions.length
      ? `${materials.length} 份材料 · ${confirmedClaims.length} 条经历 · ${sessions.length} 轮面试 · ${feedbackCount} 份反馈`
      : "还没有准备记录，从岗位定焦开始";
  }
  const completed = [
    materials.some((item) => item.kind === "job_description"),
    confirmedClaims.length > 0,
    sessions.length > 0,
    sessions.some((item) => item.has_feedback),
  ];
  const currentStep = completed.findIndex((done) => !done);
  document.querySelectorAll(".career-step").forEach((step, index) => {
    const state = completed[index] ? "complete" : index === currentStep ? "current" : "todo";
    step.dataset.state = state;
    const label = step.getAttribute("aria-label")?.replace(/，已完成$|，建议下一步$/, "") || "";
    step.setAttribute("aria-label", state === "complete" ? `${label}，已完成` : state === "current" ? `${label}，建议下一步` : label);
    if (state === "current") step.setAttribute("aria-current", "step");
    else step.removeAttribute("aria-current");
  });
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
  const chainMount = document.getElementById("career-chain-mount");
  box.replaceChildren();
  if (chainMount) {
    chainMount.replaceChildren();
    chainMount.hidden = true;
  }
  if (!messages.length) {
    if (kind === "chat") {
      const empty = document.createElement("div");
      empty.className = "chat-empty career-home";
      const intro = document.createElement("section");
      intro.className = "career-intro";
      const heading = document.createElement("h2");
      heading.textContent = "今天先推进哪一段求职证据？";
      const description = document.createElement("p");
      description.textContent = "告诉我目标岗位，或从证据链选择起点。";
      const jump = document.createElement("button");
      jump.type = "button";
      jump.className = "career-jump";
      jump.textContent = "直接开始";
      jump.onclick = () => {
        $("chat-text").scrollIntoView({ block: "center", behavior: "smooth" });
        $("chat-text").focus();
      };
      intro.append(heading, description, jump);
      empty.append(intro);
      box.append(empty);

      const path = document.createElement("section");
      path.className = "career-path";
      path.setAttribute("aria-labelledby", "career-path-title");
      const pathHead = document.createElement("div");
      pathHead.className = "career-path-head";
      const pathTitle = document.createElement("h3");
      pathTitle.id = "career-path-title";
      pathTitle.textContent = "求职证据链";
      const inventory = document.createElement("p");
      inventory.className = "career-inventory";
      inventory.textContent = "读取准备记录…";
      pathHead.append(pathTitle, inventory);
      const steps = document.createElement("div");
      steps.className = "career-steps";
      const definitions = [
        {
          number: "01",
          title: "定焦岗位",
          detail: "JD 与能力要求",
          prompt: "帮我分析一个目标岗位。请先问我岗位名称和职位描述，再把要求整理成需要证明的能力清单。",
        },
        {
          number: "02",
          title: "梳理经历",
          detail: "事实、行动与结果",
          prompt: "帮我把一段真实项目经历整理成可核实的面试回答。请先逐项问我背景、行动、结果和证据。",
        },
        {
          number: "03",
          title: "模拟面试",
          detail: "逐题追问与反馈",
          interview: true,
        },
        {
          number: "04",
          title: "回到复盘",
          detail: "定位缺口再练习",
          review: true,
        },
      ];
      for (const definition of definitions) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "career-step";
        button.setAttribute("aria-label", `${definition.title}：${definition.detail}`);
        const number = document.createElement("span");
        number.className = "career-step-number";
        number.setAttribute("aria-hidden", "true");
        number.textContent = definition.number;
        const copy = document.createElement("span");
        copy.className = "career-step-copy";
        const title = document.createElement("strong");
        title.textContent = definition.title;
        const detail = document.createElement("small");
        detail.textContent = definition.detail;
        copy.append(title, detail);
        button.append(number, copy);
        if (definition.prompt) {
          button.onclick = () => {
            $("chat-text").value = definition.prompt;
            $("chat-text").focus();
          };
        } else if (definition.interview) {
          button.onclick = async () => {
            await switchKind("interview");
            $("interview-goal").focus();
          };
        } else {
          button.onclick = async () => {
            await switchKind("interview");
            const feedback = nicoState.careerResources.sessions.find((item) => item.has_feedback);
            if (feedback) await selectSession(feedback.id);
            else {
              notice("完成一轮模拟面试后，就可以在这里复盘反馈。");
              $("interview-goal").focus();
            }
          };
        }
        steps.append(button);
      }
      const privacy = document.createElement("p");
      privacy.className = "career-privacy";
      privacy.textContent = "材料只在你明确引用后进入本轮对话。";
      path.append(pathHead, steps, privacy);
      if (chainMount) {
        chainMount.append(path);
        chainMount.hidden = false;
      }
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
  box.scrollTop = messages.length ? box.scrollHeight : 0;
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
  if (nicoState.pending.chat?.id === id) nicoState.pending.chat.controller.abort();
  const seq = ++nicoState.chatSeq;
  let errorMessage = "";
  try {
    if (nicoState.kind === "chat" && nicoState.ids.chat === id) await selectSession(id);
    notice("正在生成回复，可以继续发送补充消息。");
    await runRequest("chat", id, path, body);
  } catch (error) {
    if (error.name !== "AbortError") errorMessage = error.message;
  } finally {
    if (seq === nicoState.chatSeq && nicoState.kind === "chat" && nicoState.ids.chat === id) {
      await selectSession(id);
      if (errorMessage) notice(errorMessage);
      if ($("platform-settings").open) await loadMemoryCandidateSettings();
    }
  }
}

$("chat-retry").onclick = async () => {
  const id = nicoState.ids.chat;
  if (!id) return;
  $("chat-retry").disabled = true;
  try { await performChatRun(id, `/chat/sessions/${id}/retry`); }
  finally { $("chat-retry").disabled = !nicoState.modelReady; }
};
$("chat-memory-form").onsubmit = (event) => {
  event.preventDefault();
  const id = nicoState.ids.chat;
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
  if (!nicoState.modelReady) return;
  const text = $("chat-text").value.trim();
  if (!text || nicoState.busy.chat) return;
  let id = nicoState.ids.chat;
  setBusy("chat", true);
  let claimed;
  try {
    if (!id) {
      id = (await api("/chat/sessions", { method: "POST", body: JSON.stringify({ model_id: $("chat-model").value }) })).id;
      nicoState.ids.chat = id;
      localStorage.setItem(sessionKey("chat"), id);
    }
    claimed = await api(`/chat/sessions/${id}/messages/claim`, { method: "POST", body: JSON.stringify({ text }) });
  } catch (error) { notice(error.message); return; }
  finally { setBusy("chat", false); }
  $("chat-text").value = "";
  await performChatRun(id, `/chat/sessions/${id}/messages/run`, { version: claimed.version });
};
$("chat-interrupt").onclick = async () => {
  if (!nicoState.ids.chat) return;
  try {
    await api(`/chat/sessions/${nicoState.ids.chat}/interrupt`, { method: "POST" });
    if (nicoState.pending.chat?.id === nicoState.ids.chat) nicoState.pending.chat.controller.abort();
    await selectSession(nicoState.ids.chat);
  }
  catch (error) { notice(error.message); }
};
