/**
 * web v3 — Tool 기반 chat + Resource 첨부 + Prompt 호출
 *
 * v2 → v3 추가 사항:
 *   - GET /api/prompts 로 prompt 모달 채우기
 *   - 📋 버튼 → prompt 모달
 *   - 각 prompt 카드에 [보내기] 버튼 — 클릭 시 모달 닫고 POST /api/ask {prompt: ...}
 *   - 메인 [보내기] 는 question + attach 전용 (prompt 와 상호 배타)
 */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// 사용자가 첨부한 URI 목록 (v2 와 동일)
const attached = [];

document.addEventListener("DOMContentLoaded", async () => {
  $("#ask-form").addEventListener("submit", onSubmit);
  $("#question").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      $("#ask-form").requestSubmit();
    }
  });

  // Resource 모달 (v2 와 동일)
  $("#attach-btn").addEventListener("click", openResourceModal);
  $$("[data-close-modal]").forEach((el) =>
    el.addEventListener("click", closeResourceModal),
  );

  // Prompt 모달 (v3 신설)
  $("#prompt-btn").addEventListener("click", openPromptModal);
  $$("[data-close-prompt-modal]").forEach((el) =>
    el.addEventListener("click", closePromptModal),
  );

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeResourceModal();
      closePromptModal();
    }
  });

  // 페이지 로드 시 두 모달의 내용 미리 로드
  await Promise.all([loadResourceList(), loadPromptList()]);
});

// ════════════════════════════════════════════════════════════════
// Resource 모달 (v2 와 동일)
// ════════════════════════════════════════════════════════════════
async function loadResourceList() {
  try {
    const resp = await fetch("/api/resources");
    const data = await resp.json();
    if (data.error) {
      renderListError("static-list", data.error);
      renderListError("template-list", data.error);
      return;
    }
    renderStaticList(data.static || []);
    renderTemplateList(data.templates || []);
  } catch (err) {
    renderListError("static-list", err.message);
    renderListError("template-list", err.message);
  }
}

function renderStaticList(items) {
  const ul = $("#static-list");
  if (items.length === 0) {
    ul.innerHTML = '<li class="empty">(정적 자료 없음)</li>';
    return;
  }
  ul.innerHTML = items.map((it) => `
    <li class="resource-item">
      <div class="uri">${escapeHtml(it.uri)}</div>
      ${it.description ? `<div class="desc">${escapeHtml(it.description)}</div>` : ""}
      <div class="actions">
        <button type="button" class="add-btn" data-add-uri="${escapeHtml(it.uri)}">첨부</button>
      </div>
    </li>
  `).join("");
  ul.querySelectorAll("[data-add-uri]").forEach((btn) => {
    btn.addEventListener("click", () => {
      addAttached(btn.dataset.addUri);
      closeResourceModal();
    });
  });
}

function renderTemplateList(items) {
  const ul = $("#template-list");
  if (items.length === 0) {
    ul.innerHTML = '<li class="empty">(템플릿 자료 없음)</li>';
    return;
  }
  ul.innerHTML = items.map((it, i) => {
    const placeholders = extractPlaceholders(it.uriTemplate);
    const inputs = placeholders.map((p) =>
      `<input type="text" data-tpl-input="${i}" data-param="${escapeHtml(p)}" placeholder="${escapeHtml(p)}">`,
    ).join("");
    return `
      <li class="resource-item" data-tpl-idx="${i}">
        <div class="uri">${escapeHtml(it.uriTemplate)}</div>
        ${it.description ? `<div class="desc">${escapeHtml(it.description)}</div>` : ""}
        <div class="actions">
          ${inputs}
          <button type="button" class="add-btn" data-add-tpl="${i}" data-template="${escapeHtml(it.uriTemplate)}">첨부</button>
        </div>
      </li>
    `;
  }).join("");
  ul.querySelectorAll("[data-add-tpl]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tpl = btn.dataset.template;
      const inputs = ul.querySelectorAll(`[data-tpl-input="${btn.dataset.addTpl}"]`);
      let uri = tpl;
      let missing = null;
      inputs.forEach((inp) => {
        const v = inp.value.trim();
        if (!v) missing = inp.dataset.param;
        uri = uri.replace(`{${inp.dataset.param}}`, v);
      });
      if (missing) {
        alert(`'${missing}' 값을 입력하세요.`);
        return;
      }
      addAttached(uri);
      inputs.forEach((inp) => (inp.value = ""));
      closeResourceModal();
    });
  });
}

function renderListError(listId, msg) {
  $(`#${listId}`).innerHTML = `<li class="empty">[X] ${escapeHtml(msg)}</li>`;
}

function extractPlaceholders(template) {
  return Array.from(template.matchAll(/\{([^}]+)\}/g)).map((m) => m[1]);
}

function openResourceModal() { $("#resource-modal").classList.remove("hidden"); }
function closeResourceModal() { $("#resource-modal").classList.add("hidden"); }

// ════════════════════════════════════════════════════════════════
// Prompt 모달 (v3 신설)
// ════════════════════════════════════════════════════════════════
async function loadPromptList() {
  try {
    const resp = await fetch("/api/prompts");
    const data = await resp.json();
    if (data.error) {
      renderListError("prompt-list", data.error);
      return;
    }
    renderPromptList(data.prompts || []);
  } catch (err) {
    renderListError("prompt-list", err.message);
  }
}

function renderPromptList(items) {
  const ul = $("#prompt-list");
  if (items.length === 0) {
    ul.innerHTML = '<li class="empty">(prompt 없음)</li>';
    return;
  }
  ul.innerHTML = items.map((p, i) => {
    const argsHtml = (p.arguments && p.arguments.length > 0)
      ? `<div class="args">
          ${p.arguments.map((a) => `
            <div class="arg-row">
              <label>
                ${escapeHtml(a.name)}${a.required ? '<span class="required">*</span>' : ""}
              </label>
              <input type="text" data-prompt-arg="${i}" data-arg-name="${escapeHtml(a.name)}"
                     placeholder="${escapeHtml(a.description || a.name)}">
            </div>
          `).join("")}
         </div>`
      : `<div class="no-args">(인자 없음)</div>`;
    return `
      <li class="prompt-item" data-prompt-idx="${i}">
        <div class="name">/${escapeHtml(p.name)}</div>
        ${p.description ? `<div class="desc">${escapeHtml(p.description)}</div>` : ""}
        ${argsHtml}
        <button type="button" class="fire-btn"
                data-fire-prompt="${i}" data-name="${escapeHtml(p.name)}">보내기</button>
      </li>
    `;
  }).join("");
  ul.querySelectorAll("[data-fire-prompt]").forEach((btn) => {
    btn.addEventListener("click", () => firePrompt(btn, items));
  });
}

function firePrompt(btn, items) {
  const idx = btn.dataset.firePrompt;
  const ul = $("#prompt-list");
  // 인자 수집 — 필수 누락 시 경고
  const inputs = ul.querySelectorAll(`[data-prompt-arg="${idx}"]`);
  const promptMeta = items[Number(idx)];
  const args = {};
  let missing = null;
  inputs.forEach((inp) => {
    const name = inp.dataset.argName;
    const v = inp.value.trim();
    args[name] = v;
    const meta = (promptMeta.arguments || []).find((a) => a.name === name);
    if (meta && meta.required && !v) missing = name;
  });
  if (missing) {
    alert(`'${missing}' 인자가 필수입니다.`);
    return;
  }
  closePromptModal();
  sendRequest({ prompt: { name: btn.dataset.name, args } }, `prompt: ${btn.dataset.name}(${JSON.stringify(args)})`);
}

function openPromptModal() { $("#prompt-modal").classList.remove("hidden"); }
function closePromptModal() { $("#prompt-modal").classList.add("hidden"); }

// ════════════════════════════════════════════════════════════════
// 첨부 칩 (v2 와 동일)
// ════════════════════════════════════════════════════════════════
function addAttached(uri) {
  if (attached.includes(uri)) return;
  attached.push(uri);
  renderChips();
}

function removeAttached(uri) {
  const idx = attached.indexOf(uri);
  if (idx >= 0) attached.splice(idx, 1);
  renderChips();
}

function renderChips() {
  const el = $("#attached-chips");
  el.innerHTML = attached.map((uri) => `
    <span class="chip">
      📎 ${escapeHtml(uri)}
      <button type="button" class="chip-remove" data-remove-uri="${escapeHtml(uri)}" aria-label="제거">✕</button>
    </span>
  `).join("");
  el.querySelectorAll("[data-remove-uri]").forEach((btn) => {
    btn.addEventListener("click", () => removeAttached(btn.dataset.removeUri));
  });
}

// ════════════════════════════════════════════════════════════════
// 전송 — 두 진입점이 sendRequest 로 합류
//   메인 [보내기]: question + attach
//   prompt 카드 [보내기]: prompt + args
// ════════════════════════════════════════════════════════════════
async function onSubmit(e) {
  e.preventDefault();
  const question = $("#question").value.trim();
  if (!question) {
    alert("질문을 입력하거나, 📋 에서 prompt 를 호출하세요.");
    return;
  }
  await sendRequest(
    { question, attach: attached },
    attached.length ? `질문 + 첨부 ${attached.length}건` : "질문",
  );
}

async function sendRequest(body, kind) {
  const btn = $("#ask-btn");
  const attachBtn = $("#attach-btn");
  const promptBtn = $("#prompt-btn");
  const answerEl = $("#answer");
  const wireLogEl = $("#wire-log");

  [btn, attachBtn, promptBtn].forEach((b) => (b.disabled = true));
  btn.textContent = "처리 중...";
  answerEl.className = "answer-loading";
  answerEl.textContent = `요청 중 (${kind}) — Claude 가 답변을 만들고 있습니다...`;
  wireLogEl.textContent = "(요청 진행 중)";

  try {
    const resp = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();

    if (!resp.ok || data.error) {
      answerEl.className = "answer-error";
      answerEl.textContent = `[X] ${data.error || resp.statusText}`;
      wireLogEl.textContent = "(요청 실패)";
      return;
    }

    renderAnswer(answerEl, data);
    renderWireLog(wireLogEl, data.events || []);
  } catch (err) {
    answerEl.className = "answer-error";
    answerEl.textContent = `[X] 네트워크 오류: ${err.message}`;
    wireLogEl.textContent = "(요청 실패)";
  } finally {
    [btn, attachBtn, promptBtn].forEach((b) => (b.disabled = false));
    btn.textContent = "보내기";
  }
}

function renderAnswer(el, data) {
  el.className = "";
  el.textContent = data.answer;
  const meta = document.createElement("div");
  meta.className = "answer-meta";
  meta.textContent = `총 ${data.rounds} 라운드 — 이벤트 ${data.events?.length ?? 0}개`;
  el.appendChild(meta);
}

function renderWireLog(el, events) {
  if (events.length === 0) {
    el.textContent = "(이벤트 없음)";
    return;
  }
  const html = events.map((ev) => {
    const cls = ev.direction === ">>" ? "log-out"
              : ev.direction === "<<" ? "log-in"
              : "log-meta";
    return `<span class="${cls}">${escapeHtml(ev.direction)} ${escapeHtml(ev.text)}</span>`;
  }).join("\n");
  el.innerHTML = html;
}

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}
