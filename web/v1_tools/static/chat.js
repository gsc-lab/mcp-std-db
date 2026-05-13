/**
 * web v1 — Tool 기반 chat 프론트엔드
 *
 * 동작:
 *   POST /api/ask {question} → {answer, rounds, events}
 *   events 는 백엔드가 모은 통신 로그. 화면 하단 패널에 표시.
 *
 * 의존성 없음 (vanilla JS, fetch + DOM).
 */

const $ = (sel) => document.querySelector(sel);

document.addEventListener("DOMContentLoaded", () => {
  $("#ask-form").addEventListener("submit", onSubmit);
  // Ctrl+Enter / Cmd+Enter 로도 전송
  $("#question").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      $("#ask-form").requestSubmit();
    }
  });
});

async function onSubmit(e) {
  e.preventDefault();
  const question = $("#question").value.trim();
  if (!question) return;

  const btn = $("#ask-btn");
  const answerEl = $("#answer");
  const wireLogEl = $("#wire-log");

  // 요청 시작
  btn.disabled = true;
  btn.textContent = "처리 중...";
  answerEl.className = "answer-loading";
  answerEl.textContent = "Claude 가 도구를 호출하며 답변을 만들고 있습니다...";
  wireLogEl.textContent = "(요청 진행 중)";

  try {
    const resp = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
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
  // 방향(>>, <<, **) 마다 색을 입혀 가독성 향상
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
