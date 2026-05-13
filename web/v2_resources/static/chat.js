/**
 * web v2 — Tool 기반 chat + Resource 사용자 첨부
 *
 * v1 → v2 추가 사항:
 *   - 페이지 로드 시 GET /api/resources 로 모달 채우기
 *   - 📎 버튼 → 모달 → 자료 선택 → attached 배열 push → 칩 표시
 *   - POST /api/ask 본문에 {question, attach: [...]} 전송
 *
 * 상태: in-memory 배열 `attached` 가 곧 상태. DOM 이 곧 view.
 */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// 사용자가 첨부한 URI 목록. 보낼 때 함께 전송.
const attached = [];

document.addEventListener("DOMContentLoaded", async () => {
  $("#ask-form").addEventListener("submit", onSubmit);
  $("#question").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      $("#ask-form").requestSubmit();
    }
  });
  $("#attach-btn").addEventListener("click", openModal);
  $$("[data-close-modal]").forEach((el) => el.addEventListener("click", closeModal));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeModal();
  });

  // 페이지 로드 시 한 번만 모달 내용 로드 (캐시).
  await loadResourceList();
});

// ────────────────────────────────────────────────────────────────────
// 모달 채우기
// ────────────────────────────────────────────────────────────────────
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
      closeModal();
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
    // URI 템플릿의 {param} 자리를 입력칸으로 표시
    const placeholders = extractPlaceholders(it.uriTemplate);
    const inputs = placeholders.map((p) =>
      `<input type="text" data-tpl-input="${i}" data-param="${escapeHtml(p)}" placeholder="${escapeHtml(p)}">`
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
      closeModal();
    });
  });
}

function renderListError(listId, msg) {
  $(`#${listId}`).innerHTML = `<li class="empty">[X] ${escapeHtml(msg)}</li>`;
}

function extractPlaceholders(template) {
  return Array.from(template.matchAll(/\{([^}]+)\}/g)).map((m) => m[1]);
}

// ────────────────────────────────────────────────────────────────────
// 모달 열기/닫기
// ────────────────────────────────────────────────────────────────────
function openModal() { $("#resource-modal").classList.remove("hidden"); }
function closeModal() { $("#resource-modal").classList.add("hidden"); }

// ────────────────────────────────────────────────────────────────────
// 첨부 칩 관리
// ────────────────────────────────────────────────────────────────────
function addAttached(uri) {
  if (attached.includes(uri)) return;  // 중복 방지
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

// ────────────────────────────────────────────────────────────────────
// 전송 (v1 과 동일하되 attach 배열 추가)
// ────────────────────────────────────────────────────────────────────
async function onSubmit(e) {
  e.preventDefault();
  const question = $("#question").value.trim();
  if (!question) return;

  const btn = $("#ask-btn");
  const answerEl = $("#answer");
  const wireLogEl = $("#wire-log");

  btn.disabled = true;
  btn.textContent = "처리 중...";
  answerEl.className = "answer-loading";
  answerEl.textContent = attached.length
    ? `자료 ${attached.length}건 첨부 — Claude 가 답변을 만들고 있습니다...`
    : "Claude 가 답변을 만들고 있습니다...";
  wireLogEl.textContent = "(요청 진행 중)";

  try {
    const resp = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, attach: attached }),
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
    btn.disabled = false;
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
