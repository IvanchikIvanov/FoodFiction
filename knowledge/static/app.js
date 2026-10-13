"use strict";

const $ = (id) => document.getElementById(id);
const state = {status: null, busy: false, controller: null, sourceController: null, recognition: null, listening: false, speechButton: null, history: []};
const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
const mobileLayout = window.matchMedia("(max-width: 760px)");
try {
  const savedLanguage = localStorage.getItem("food-fiction-language");
  if (Array.from($("language").options).some((item) => item.value === savedLanguage)) $("language").value = savedLanguage;
} catch { /* The selected language still works when browser storage is blocked. */ }

function node(tag, className = "", text = "") {
  const result = document.createElement(tag);
  result.className = className;
  if (text) result.textContent = text;
  return result;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(svg.namespaceURI, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

function notify(message) {
  $("notice").textContent = message;
  $("notice").hidden = !message;
}

function resizeInput() {
  $("question").style.height = "auto";
  $("question").style.height = `${Math.min($("question").scrollHeight, 144)}px`;
  $("send-button").disabled = state.busy || !$("question").value.trim();
}

async function api(path, options = {}) {
  const response = await fetch(path, {credentials: "same-origin", ...options,
    headers: {"Content-Type": "application/json", ...(options.headers || {})}});
  let body;
  try { body = await response.json(); } catch { throw new Error("Сервис не ответил. Проверьте соединение и повторите вопрос."); }
  if (!response.ok) {
    const error = new Error(typeof body.detail === "string" ? body.detail : "Не удалось выполнить запрос. Попробуйте ещё раз.");
    error.status = response.status;
    throw error;
  }
  return body;
}

async function refreshStatus() {
  state.status = await api("/api/web/status");
  $("mode-banner").hidden = !state.status.preview;
  $("source-state").textContent = state.status.internet_mode ? "База знаний + интернет" : state.status.preview ? "Учебные материалы" : state.status.authenticated ? "Рабочая база" : "Доступ по коду";
  $("logout").hidden = state.status.preview || !state.status.authenticated;
  $("login-panel").hidden = state.status.authenticated || !state.status.configured;
  if (!state.status.configured) notify("Рабочая база ещё не подключена. Доступ появится после настройки ответственным сотрудником.");
}

function setMenu(open) {
  open = open && mobileLayout.matches;
  $("sidebar").classList.toggle("open", open);
  $("sidebar-scrim").hidden = !open;
  $("menu-button").setAttribute("aria-expanded", String(open));
  $("sidebar").inert = mobileLayout.matches && !open;
  document.querySelector(".main-shell").inert = open;
  if (open) $("new-chat").focus();
}

function stopSpeech() {
  if ("speechSynthesis" in window) window.speechSynthesis.cancel();
  if (state.speechButton) {
    state.speechButton.setAttribute("aria-label", "Прослушать фрагмент");
    state.speechButton.setAttribute("aria-pressed", "false");
    state.speechButton = null;
  }
}

function newQuestion() {
  state.controller?.abort();
  state.sourceController?.abort();
  state.controller = null;
  state.busy = false;
  state.history = [];
  state.recognition?.abort();
  state.recognition = null;
  recordingState(false);
  stopSpeech();
  $("conversation").replaceChildren();
  $("conversation").hidden = true;
  $("welcome").hidden = false;
  $("question").value = "";
  notify("");
  setMenu(false);
  resizeInput();
  $("question").focus();
  window.scrollTo({top: 0, behavior: "instant"});
}

function loadingMessage() {
  const message = node("article", "message answer-message");
  message.setAttribute("aria-busy", "true");
  const label = node("div", "answer-label", "Формируем ответ…");
  const lines = node("div", "loading-lines");
  lines.setAttribute("aria-hidden", "true");
  for (let i = 0; i < 3; i++) lines.append(node("span"));
  message.append(label, lines);
  return message;
}

function actionButton(name, label, fn) {
  const button = node("button", "icon-button");
  button.type = "button";
  button.setAttribute("aria-label", label);
  button.title = label;
  button.append(icon(name));
  button.addEventListener("click", () => fn(button));
  return button;
}

function speak(text, button, answerLanguage) {
  if (state.speechButton === button) { stopSpeech(); return; }
  stopSpeech();
  if (!("speechSynthesis" in window)) { notify("На этом устройстве озвучка недоступна. Ответ можно прочитать на экране."); return; }
  const language = answerLanguage.split("-")[0].toLowerCase();
  const voices = window.speechSynthesis.getVoices();
  const voice = voices.find((item) => item.lang.toLowerCase() === answerLanguage.toLowerCase())
    || voices.find((item) => item.lang.toLowerCase().split("-")[0] === language);
  if (!voice) { notify("На устройстве нет голоса для языка этой инструкции. Текст остаётся доступным для чтения."); return; }
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.voice = voice;
  utterance.lang = voice.lang;
  utterance.rate = 0.95;
  state.speechButton = button;
  button.setAttribute("aria-label", "Остановить озвучку");
  button.setAttribute("aria-pressed", "true");
  utterance.onend = () => { if (state.speechButton === button) stopSpeech(); };
  utterance.onerror = () => { if (state.speechButton === button) stopSpeech(); };
  window.speechSynthesis.speak(utterance);
}

async function openSource(item) {
  state.sourceController?.abort();
  const controller = new AbortController();
  state.sourceController = controller;
  $("source-title").textContent = item.title;
  $("source-body").replaceChildren(node("p", "", "Открываем инструкцию…"));
  $("source-dialog").showModal();
  try {
    const result = await api(`/api/web/documents/${encodeURIComponent(item.scope)}/${encodeURIComponent(item.document_id)}`, {signal: controller.signal});
    if (controller.signal.aborted) return;
    const body = $("source-body");
    body.replaceChildren();
    if (result.fingerprint !== item.fingerprint) body.append(node("p", "inline-notice", "Инструкция обновилась после вашего вопроса. Ниже — её актуальная версия."));
    if (result.url) {
      const link = node("a", "external-source", "Открыть оригинал в Битрикс24 ↗");
      link.href = result.url; link.target = "_blank"; link.rel = "noopener noreferrer";
      body.append(link);
    }
    appendArticlePhotos(body, result.images || [], result.url, result.title);
    result.pages.forEach((page, index) => {
      body.append(node("div", "source-page-label", item.scope.startsWith("disk:") ? `Страница ${index + 1}` : "Текст статьи"), node("p", "source-page", page));
    });
  } catch (error) {
    if (error.name !== "AbortError") $("source-body").replaceChildren(node("p", "inline-notice", error.message));
  }
}

function appendArticlePhotos(parent, images, sourceUrl, title) {
  const available = images.filter((item) => typeof item.url === "string" && item.url.startsWith("/api/web/images/")).slice(0, 12);
  if (!available.length) return 0;
  const section = node("section", "article-photos");
  section.setAttribute("aria-label", `Фотографии из статьи: ${title}`);
  const caption = node("p", "article-photos-caption", `Фотографии из статьи «${title}»`);
  const gallery = node("div", "article-photo-grid");
  for (const item of available) {
    const frame = node(sourceUrl ? "a" : "div", "article-photo");
    if (sourceUrl) {
      frame.href = sourceUrl; frame.target = "_blank"; frame.rel = "noopener noreferrer";
      frame.setAttribute("aria-label", `Открыть статью в Битрикс24: ${title}`);
    }
    const image = node("img");
    image.src = item.url; image.alt = item.alt || title;
    image.loading = "lazy"; image.decoding = "async";
    image.addEventListener("error", () => { frame.remove(); if (!gallery.children.length) section.remove(); });
    frame.append(image); gallery.append(frame);
  }
  section.append(caption, gallery); parent.append(section);
  return available.length;
}

function renderAnswer(message, response, answerLanguage) {
  message.removeAttribute("aria-busy");
  message.replaceChildren();
  const label = node("div", "answer-label");
  const mark = node("span", "answer-mark");
  mark.append(icon("chat"));
  label.append(mark, node("span", "", "Помощник"), node("span", "answer-type", response.generated ? "Ответ нейросети" : "По базе знаний"));
  const text = response.answer || "Ответ не получен. Попробуйте повторить вопрос.";
  const answerText = node("p", "answer-text");
  appendSourceText(answerText, text, response.results);
  message.append(label, answerText);
  if (response.notice) message.append(node("p", "answer-notice", response.notice));
  const actions = node("div", "answer-actions");
  const speech = actionButton("volume", "Прослушать ответ", (button) => speak(text, button, answerLanguage));
  speech.setAttribute("aria-pressed", "false");
  actions.append(speech, actionButton("copy", "Скопировать текст", async () => {
    try { await navigator.clipboard.writeText(text); $("announcer").textContent = "Текст скопирован"; notify("Текст скопирован."); }
    catch { notify("Не удалось скопировать автоматически. Выделите текст и скопируйте его вручную."); }
  }), node("span", "answer-action-label", response.generated
    ? (response.results.length ? "Ответ по материалам базы знаний" : "Сообщение помощника")
    : (response.results.length ? "Текст из базы знаний" : "Сообщение помощника")));
  message.append(actions);
  if (response.results.length) {
    const sources = node("div", "sources");
    const seen = new Set();
    response.results.forEach((item, index) => {
      const key = `${item.scope}/${item.document_id}`;
      if (seen.has(key)) return;
      seen.add(key);
      const button = node("button", "source-button");
      button.append(icon("file"), node("span", "", `[${index + 1}] ${item.title}`), node("small", "", item.scope.startsWith("disk:") ? `Стр. ${item.page}` : "Статья"), icon("right"));
      button.addEventListener("click", () => openSource(item));
      sources.append(button);
    });
    message.append(sources);
    const cited = new Set(Array.from(text.matchAll(/\[(\d+)\]/g), (match) => Number(match[1]) - 1));
    const illustrated = new Set();
    let photoCount = 0;
    for (const index of cited) {
      const item = response.results[index];
      if (!item || photoCount >= 12) continue;
      const key = `${item.scope}/${item.document_id}`;
      if (illustrated.has(key)) continue;
      illustrated.add(key);
      photoCount += appendArticlePhotos(message, (item.images || []).slice(0, 12 - photoCount), item.url, item.title);
    }
  }
  $("announcer").textContent = response.results.length ? "Ответ готов. Источники доступны ниже." : "Помощник ответил.";
}

function appendSourceText(parent, text, results) {
  let cursor = 0;
  for (const match of text.matchAll(/\[(\d+)\]/g)) {
    appendLinkedText(parent, text.slice(cursor, match.index));
    const item = results[Number(match[1]) - 1];
    if (item) {
      const button = node("button", "source-citation", match[0]);
      button.type = "button";
      button.title = item.title;
      button.setAttribute("aria-label", `Открыть источник ${match[1]}: ${item.title}`);
      button.addEventListener("click", () => openSource(item));
      parent.append(button);
    } else {
      parent.append(document.createTextNode(match[0]));
    }
    cursor = match.index + match[0].length;
  }
  appendLinkedText(parent, text.slice(cursor));
}

function appendLinkedText(parent, text) {
  const pattern = /\[([^\]\n]{1,160})\]\((https:\/\/[^\s)]+)\)|(https:\/\/[^\s<]+)/g;
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    parent.append(document.createTextNode(text.slice(cursor, match.index)));
    const rawUrl = match[2] || match[3];
    const trailing = match[2] ? "" : (rawUrl.match(/[.,;:!?]+$/)?.[0] || "");
    const url = trailing ? rawUrl.slice(0, -trailing.length) : rawUrl;
    const link = node("a", "answer-link", match[1] || url);
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    parent.append(link);
    if (trailing) parent.append(document.createTextNode(trailing));
    cursor = match.index + match[0].length;
  }
  parent.append(document.createTextNode(text.slice(cursor)));
}

async function ask(question) {
  if (!question || state.busy) return;
  if (!state.status?.authenticated) {
    if (state.status?.configured) { $("login-panel").hidden = false; $("access-code").focus(); notify("Сначала войдите в базу компании. Ваш вопрос останется в поле ввода."); }
    else notify("Рабочая база ещё не подключена. Вопрос можно отправить после настройки доступа.");
    return;
  }
  state.recognition?.stop();
  stopSpeech();
  notify("");
  state.busy = true;
  const answerLanguage = $("language").value;
  const controller = new AbortController();
  state.controller = controller;
  $("welcome").hidden = true;
  $("conversation").hidden = false;
  const user = node("article", "message question-message");
  user.append(node("p", "question-bubble", question));
  const loading = loadingMessage();
  $("conversation").append(user, loading);
  $("question").value = "";
  resizeInput();
  user.scrollIntoView({behavior: "instant", block: "start"});
  try {
    const result = await api("/api/web/search", {method: "POST", body: JSON.stringify({query: question, history: state.history.slice(-8), language: answerLanguage}), signal: controller.signal});
    renderAnswer(loading, result, answerLanguage);
    state.history.push({role: "user", content: question}, {role: "assistant", content: result.answer});
    state.history = state.history.slice(-8);
  } catch (error) {
    if (error.name !== "AbortError") {
      loading.removeAttribute("aria-busy");
      loading.replaceChildren(node("p", "inline-notice", error.message));
      $("question").value = question;
      if (error.status === 401) { await refreshStatus(); $("login-panel").hidden = false; }
    }
  } finally {
    if (state.controller === controller) {
      state.busy = false;
      state.controller = null;
      resizeInput();
    }
  }
}

function recordingState(listening) {
  state.listening = listening;
  $("recording-status").hidden = !listening;
  $("voice-start").classList.toggle("is-recording", listening);
  $("voice-start").querySelector("strong").textContent = listening ? "Слушаю ваш вопрос…" : "Задать вопрос голосом";
  $("mic-button").setAttribute("aria-label", listening ? "Завершить диктовку" : "Продиктовать вопрос");
}

function startDictation() {
  if (state.listening) { state.recognition?.stop(); return; }
  if (state.busy) { notify("Дождитесь результата поиска, затем задайте следующий вопрос."); return; }
  if (!Recognition) { notify("Этот браузер не поддерживает диктовку. Напишите вопрос или откройте сайт в браузере с голосовым вводом."); $("question").focus(); return; }
  stopSpeech();
  notify("Диктовка использует сервис вашего браузера. После распознавания вопрос отправится автоматически.");
  const recognition = new Recognition();
  let shouldSubmit = false;
  state.recognition = recognition;
  recognition.lang = $("language").value;
  recognition.interimResults = true;
  recognition.continuous = false;
  recognition.onstart = () => recordingState(true);
  recognition.onresult = (event) => {
    if (state.recognition !== recognition) return;
    $("question").value = Array.from(event.results, (result) => result[0].transcript).join(" ").slice(0, 1000);
    shouldSubmit = Array.from(event.results).some((result) => result.isFinal);
    resizeInput();
  };
  recognition.onend = () => {
    if (state.recognition === recognition) {
      const question = $("question").value.trim();
      recordingState(false);
      state.recognition = null;
      if (shouldSubmit && question) setTimeout(() => ask(question), 0);
    }
  };
  recognition.onerror = (event) => {
    const messages = {"not-allowed": "Разрешите доступ к микрофону в настройках браузера или напишите вопрос.", "no-speech": "Речь не распознана. Попробуйте ещё раз или напишите вопрос.", "language-not-supported": "Диктовка на этом языке недоступна в вашем браузере. Можно ввести вопрос текстом.", "network": "Сервис диктовки недоступен. Проверьте соединение или напишите вопрос."};
    if (event.error !== "aborted") notify(messages[event.error] || "Не удалось распознать речь. Попробуйте ещё раз или напишите вопрос.");
  };
  try { recognition.start(); } catch { recordingState(false); state.recognition = null; notify("Не удалось включить микрофон. Попробуйте ещё раз."); }
}

$("question-form").addEventListener("submit", (event) => { event.preventDefault(); ask($("question").value.trim()); });
$("question").addEventListener("input", resizeInput);
window.addEventListener("resize", resizeInput);
$("question").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); ask($("question").value.trim()); } });
document.querySelectorAll(".example").forEach((button) => button.addEventListener("click", () => {
  if (state.busy) return;
  $("question").value = button.dataset.question;
  resizeInput();
  ask(button.dataset.question);
}));
$("new-chat").addEventListener("click", newQuestion);
$("home-nav").addEventListener("click", newQuestion);
$("help-nav").addEventListener("click", () => { setMenu(false); $("help-dialog").showModal(); });
$("menu-button").addEventListener("click", () => setMenu(!$("sidebar").classList.contains("open")));
$("sidebar-scrim").addEventListener("click", () => setMenu(false));
document.addEventListener("keydown", (event) => { if (event.key === "Escape" && $("sidebar").classList.contains("open")) { setMenu(false); $("menu-button").focus(); } });
document.querySelectorAll(".dialog-close").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
$("source-dialog").addEventListener("close", () => state.sourceController?.abort());
$("voice-start").addEventListener("click", startDictation);
$("mic-button").addEventListener("click", startDictation);
$("stop-recording").addEventListener("click", () => state.recognition?.stop());
$("language").addEventListener("change", () => {
  state.recognition?.abort();
  stopSpeech();
  try { localStorage.setItem("food-fiction-language", $("language").value); } catch { /* Storage may be blocked. */ }
  notify("Помощник будет отвечать на выбранном языке, независимо от языка вопроса.");
});
$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  $("login-error").hidden = true;
  try {
    await api("/api/web/login", {method: "POST", body: JSON.stringify({code: $("access-code").value})});
    $("access-code").value = "";
    await refreshStatus();
    notify("Вход сохранён в этом браузере на 90 дней. Теперь можно задать вопрос.");
    $("question").focus();
  } catch (error) { $("login-error").textContent = error.message; $("login-error").hidden = false; }
  finally { button.disabled = false; }
});
$("logout").addEventListener("click", async () => {
  try { await api("/api/web/logout", {method: "POST"}); newQuestion(); await refreshStatus(); }
  catch { notify("Не удалось выйти. Проверьте соединение и повторите попытку."); }
});
window.addEventListener("pagehide", () => { state.recognition?.abort(); stopSpeech(); });
if ("speechSynthesis" in window) window.speechSynthesis.getVoices();
if (!Recognition) $("voice-caption").textContent = "В этом браузере доступен текстовый ввод";
refreshStatus().catch(() => { $("source-state").textContent = "Нет подключения"; notify("Не удалось подключиться к помощнику. Проверьте соединение и обновите страницу."); });
resizeInput();
mobileLayout.addEventListener("change", () => setMenu(false));
setMenu(false);
