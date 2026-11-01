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
$("interview-setup").onsubmit = (event) => {
  event.preventDefault();
  if (!state.modelReady) return;
  action("interview", async () => {
    const goal = $("interview-goal").value.trim();
    if (!goal) return;
    const max_questions = Number($("interview-max-questions").value);
    const material_ids = [...$("material-list").querySelectorAll("input:checked")].map((item) => item.value);
    const claim_ids = [...$("claim-list").querySelectorAll("input:checked")].map((item) => item.value);
    const session = await api("/interview/sessions", { method: "POST", body: JSON.stringify({ goal, max_questions, material_ids, claim_ids, model_id: $("interview-model").value }) });
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
