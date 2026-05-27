/**
 * web v3 — 대화 메모리 프론트엔드
 *
 * v2 와의 차이:
 *   - session_id 를 localStorage 에 보관, 모든 요청 본문에 동봉
 *   - 답변을 단일 영역이 아닌 *대화 스레드* 에 누적 (메모리 동작의 시각적 증거)
 *   - "새 대화" 버튼 → /api/reset + 스레드 비우기 + 새 session_id
 *
 * 백엔드(Redis)가 session_id 별로 대화를 기억하므로, 후속 질문이 이전 맥락을 인지.
 */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const attached = [];

// 세션 ID — localStorage 에 보관, 없으면 생성.
function getSessionId() {
  let id = localStorage.getItem("session_id");
  if (!id) {
    id = crypto.randomUUID();
    localStorage.setItem("session_id", id);
  }
  return id;
}

document.addEventListener("DOMContentLoaded", async () => {
  $("#session-label").textContent = `세션: ${getSessionId().slice(0, 8)}…`;

  $("#ask-form").addEventListener("submit", onSubmit);
  $("#question").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      $("#ask-form").requestSubmit();
    }
  });
  $("#attach-btn").addEventListener("click", openResourceModal);
  $$("[data-close-modal]").forEach((el) => el.addEventListener("click", closeResourceModal));
  $("#prompt-btn").addEventListener("click", openPromptModal);
  $$("[data-close-prompt-modal]").forEach((el) => el.addEventListener("click", closePromptModal));
  $("#reset-btn").addEventListener("click", onReset);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeResourceModal(); closePromptModal(); }
  });

  await Promise.all([loadResourceList(), loadPromptList()]);
});

// ════════════════════════════════════════════════════════════════
// 새 대화 — 세션 초기화
// ════════════════════════════════════════════════════════════════
async function onReset() {
  const oldId = getSessionId();
  try {
    await fetch("/api/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: oldId }),
    });
  } catch { /* 무시 — 어차피 새 세션 발급 */ }

  localStorage.removeItem("session_id");
  const newId = getSessionId();
  $("#session-label").textContent = `세션: ${newId.slice(0, 8)}…`;
  $("#thread").innerHTML = '<div class="thread-empty">새 대화를 시작했습니다. 이전 맥락은 비워졌어요.</div>';
}

// ════════════════════════════════════════════════════════════════
// 대화 스레드 렌더링
// ════════════════════════════════════════════════════════════════
function clearThreadEmpty() {
  const empty = $("#thread .thread-empty");
  if (empty) empty.remove();
}

function addBubble(role, text, extraClass = "") {
  clearThreadEmpty();
  const div = document.createElement("div");
  div.className = `bubble bubble-${role} ${extraClass}`.trim();
  div.textContent = text;
  $("#thread").appendChild(div);
  $("#thread").scrollTop = $("#thread").scrollHeight;
  return div;
}

// ════════════════════════════════════════════════════════════════
// Resource 모달 (v2 와 동일)
// ════════════════════════════════════════════════════════════════
async function loadResourceList() {
  try {
    const resp = await fetch("/api/resources");
    const data = await resp.json();
    if (data.detail || data.error) {
      const m = data.detail || data.error;
      renderListError("static-list", m); renderListError("template-list", m); return;
    }
    renderStaticList(data.static || []);
    renderTemplateList(data.templates || []);
  } catch (err) {
    renderListError("static-list", err.message); renderListError("template-list", err.message);
  }
}

function renderStaticList(items) {
  const ul = $("#static-list");
  if (items.length === 0) { ul.innerHTML = '<li class="empty">(정적 자료 없음)</li>'; return; }
  ul.innerHTML = items.map((it) => `
    <li class="resource-item">
      <div class="uri">${escapeHtml(it.uri)}</div>
      ${it.description ? `<div class="desc">${escapeHtml(it.description)}</div>` : ""}
      <div class="actions"><button type="button" class="add-btn" data-add-uri="${escapeHtml(it.uri)}">첨부</button></div>
    </li>`).join("");
  ul.querySelectorAll("[data-add-uri]").forEach((btn) => {
    btn.addEventListener("click", () => { addAttached(btn.dataset.addUri); closeResourceModal(); });
  });
}

function renderTemplateList(items) {
  const ul = $("#template-list");
  if (items.length === 0) { ul.innerHTML = '<li class="empty">(템플릿 자료 없음)</li>'; return; }
  ul.innerHTML = items.map((it, i) => {
    const placeholders = extractPlaceholders(it.uriTemplate);
    const inputs = placeholders.map((p) =>
      `<input type="text" data-tpl-input="${i}" data-param="${escapeHtml(p)}" placeholder="${escapeHtml(p)}">`).join("");
    return `
      <li class="resource-item" data-tpl-idx="${i}">
        <div class="uri">${escapeHtml(it.uriTemplate)}</div>
        ${it.description ? `<div class="desc">${escapeHtml(it.description)}</div>` : ""}
        <div class="actions">${inputs}
          <button type="button" class="add-btn" data-add-tpl="${i}" data-template="${escapeHtml(it.uriTemplate)}">첨부</button>
        </div>
      </li>`;
  }).join("");
  ul.querySelectorAll("[data-add-tpl]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tpl = btn.dataset.template;
      const inputs = ul.querySelectorAll(`[data-tpl-input="${btn.dataset.addTpl}"]`);
      let uri = tpl, missing = null;
      inputs.forEach((inp) => {
        const v = inp.value.trim();
        if (!v) missing = inp.dataset.param;
        uri = uri.replace(`{${inp.dataset.param}}`, v);
      });
      if (missing) { alert(`'${missing}' 값을 입력하세요.`); return; }
      addAttached(uri);
      inputs.forEach((inp) => (inp.value = ""));
      closeResourceModal();
    });
  });
}

function renderListError(listId, msg) { $(`#${listId}`).innerHTML = `<li class="empty">[X] ${escapeHtml(msg)}</li>`; }
function extractPlaceholders(t) { return Array.from(t.matchAll(/\{([^}]+)\}/g)).map((m) => m[1]); }
function openResourceModal() { $("#resource-modal").classList.remove("hidden"); }
function closeResourceModal() { $("#resource-modal").classList.add("hidden"); }

// ════════════════════════════════════════════════════════════════
// Prompt 모달 (v2 와 동일하되, 메모리 미적용 — 스레드에 별도 표시)
// ════════════════════════════════════════════════════════════════
async function loadPromptList() {
  try {
    const resp = await fetch("/api/prompts");
    const data = await resp.json();
    if (data.detail || data.error) { renderListError("prompt-list", data.detail || data.error); return; }
    renderPromptList(data.prompts || []);
  } catch (err) { renderListError("prompt-list", err.message); }
}

function renderPromptList(items) {
  const ul = $("#prompt-list");
  if (items.length === 0) { ul.innerHTML = '<li class="empty">(prompt 없음)</li>'; return; }
  ul.innerHTML = items.map((p, i) => {
    const argsHtml = (p.arguments && p.arguments.length > 0)
      ? `<div class="args">${p.arguments.map((a) => `
            <div class="arg-row">
              <label>${escapeHtml(a.name)}${a.required ? '<span class="required">*</span>' : ""}</label>
              <input type="text" data-prompt-arg="${i}" data-arg-name="${escapeHtml(a.name)}"
                     placeholder="${escapeHtml(a.description || a.name)}">
            </div>`).join("")}</div>`
      : `<div class="no-args">(인자 없음)</div>`;
    return `
      <li class="prompt-item" data-prompt-idx="${i}">
        <div class="name">/${escapeHtml(p.name)}</div>
        ${p.description ? `<div class="desc">${escapeHtml(p.description)}</div>` : ""}
        ${argsHtml}
        <button type="button" class="fire-btn" data-fire-prompt="${i}" data-name="${escapeHtml(p.name)}">보내기</button>
      </li>`;
  }).join("");
  ul.querySelectorAll("[data-fire-prompt]").forEach((btn) => {
    btn.addEventListener("click", () => firePrompt(btn, items));
  });
}

function firePrompt(btn, items) {
  const idx = btn.dataset.firePrompt;
  const ul = $("#prompt-list");
  const inputs = ul.querySelectorAll(`[data-prompt-arg="${idx}"]`);
  const meta = items[Number(idx)];
  const args = {}; let missing = null;
  inputs.forEach((inp) => {
    const name = inp.dataset.argName;
    const v = inp.value.trim();
    args[name] = v;
    const m = (meta.arguments || []).find((a) => a.name === name);
    if (m && m.required && !v) missing = name;
  });
  if (missing) { alert(`'${missing}' 인자가 필수입니다.`); return; }
  closePromptModal();
  // prompt 는 메모리 미적용 — 본문에 session_id 안 보냄
  sendRequest(
    { prompt: { name: btn.dataset.name, args } },
    `📋 ${btn.dataset.name}(${JSON.stringify(args)})`,
  );
}

function openPromptModal() { $("#prompt-modal").classList.remove("hidden"); }
function closePromptModal() { $("#prompt-modal").classList.add("hidden"); }

// ════════════════════════════════════════════════════════════════
// 첨부 chips (v2 와 동일)
// ════════════════════════════════════════════════════════════════
function addAttached(uri) { if (!attached.includes(uri)) { attached.push(uri); renderChips(); } }
function removeAttached(uri) { const i = attached.indexOf(uri); if (i >= 0) attached.splice(i, 1); renderChips(); }
function renderChips() {
  const el = $("#attached-chips");
  el.innerHTML = attached.map((uri) => `
    <span class="chip">📎 ${escapeHtml(uri)}
      <button type="button" class="chip-remove" data-remove-uri="${escapeHtml(uri)}" aria-label="제거">✕</button>
    </span>`).join("");
  el.querySelectorAll("[data-remove-uri]").forEach((btn) =>
    btn.addEventListener("click", () => removeAttached(btn.dataset.removeUri)));
}

// ════════════════════════════════════════════════════════════════
// 전송
// ════════════════════════════════════════════════════════════════
async function onSubmit(e) {
  e.preventDefault();
  const question = $("#question").value.trim();
  if (!question) { alert("질문을 입력하거나, 📋 에서 prompt 를 호출하세요."); return; }
  $("#question").value = "";
  // 일반 질문 — session_id 동봉 (메모리 적용)
  await sendRequest(
    { session_id: getSessionId(), question, attach: attached },
    question,
  );
}

async function sendRequest(body, userLabel) {
  const btn = $("#ask-btn"), attachBtn = $("#attach-btn"), promptBtn = $("#prompt-btn");
  const wireLogEl = $("#wire-log");

  // 사용자 버블 추가
  addBubble("user", userLabel);
  const loadingBubble = addBubble("assistant", "답변 생성 중...", "bubble-loading");

  [btn, attachBtn, promptBtn].forEach((b) => (b.disabled = true));
  btn.textContent = "처리 중...";
  wireLogEl.textContent = "(요청 진행 중)";

  try {
    const resp = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();

    loadingBubble.remove();
    if (!resp.ok || data.detail || data.error) {
      addBubble("assistant", `[X] ${data.detail || data.error || resp.statusText}`, "bubble-error");
      wireLogEl.textContent = "(요청 실패)";
      return;
    }

    const b = addBubble("assistant", data.answer);
    const meta = document.createElement("div");
    meta.className = "bubble-meta";
    meta.textContent = `${data.rounds} 라운드 · 이벤트 ${data.events?.length ?? 0}개`;
    b.appendChild(meta);

    renderWireLog(wireLogEl, data.events || []);
  } catch (err) {
    loadingBubble.remove();
    addBubble("assistant", `[X] 네트워크 오류: ${err.message}`, "bubble-error");
    wireLogEl.textContent = "(요청 실패)";
  } finally {
    [btn, attachBtn, promptBtn].forEach((b) => (b.disabled = false));
    btn.textContent = "보내기";
  }
}

function renderWireLog(el, events) {
  if (events.length === 0) { el.textContent = "(이벤트 없음)"; return; }
  el.innerHTML = events.map((ev) => {
    const cls = ev.direction === ">>" ? "log-out" : ev.direction === "<<" ? "log-in" : "log-meta";
    return `<span class="${cls}">${escapeHtml(ev.direction)} ${escapeHtml(ev.text)}</span>`;
  }).join("\n");
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}
