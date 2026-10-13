"use strict";
const $ = (id) => document.getElementById(id);
const state = {busy:false, recorder:null, stream:null, context:null, timer:null, interval:null, history:[], ready:false};
const labels = {"ru-RU":"Задать вопрос голосом","en-US":"Ask a question aloud","uz-UZ":"Ovoz bilan savol bering","tg-TJ":"Бо овоз савол диҳед","kk-KZ":"Дауыстап сұрақ қойыңыз","hy-AM":"Հարց տվեք ձայնով","az-AZ":"Səslə sual verin","tr-TR":"Sesli soru sorun","zh-CN":"语音提问"};
let currentLabel = labels["ru-RU"];
function node(tag, className, text="") { const el=document.createElement(tag); el.className=className; el.textContent=text; return el; }
function status(text) { $("voice-status").textContent=text; }
function button(mode="idle") {
  $("voice-button").disabled=mode==="busy" || !state.ready;
  $("voice-button").classList.toggle("listening",mode==="listening");
  $("voice-button").classList.toggle("busy",mode==="busy");
  $("voice-button").setAttribute("aria-pressed",String(mode==="listening"));
  $("button-label").textContent=mode==="listening"?"Слушаю… Нажмите, чтобы закончить":mode==="busy"?"Готовлю ответ…":currentLabel;
}
async function api(path,options={}) {
  const response=await fetch(path,{credentials:"same-origin",...options});
  let body; try { body=await response.json(); } catch { throw new Error("Сервис не ответил. Попробуйте ещё раз."); }
  if(!response.ok) throw new Error(typeof body.detail==="string"?body.detail:"Не удалось выполнить запрос. Попробуйте ещё раз.");
  return body;
}
function safeSource(url) {
  try { const u=new URL(url); return u.protocol==="https:" && !u.username && !u.password && !u.search && !u.hash && u.hostname.endsWith(".bitrix24.ru") && u.pathname.startsWith("/knowledge/")?u.href:null; } catch { return null; }
}
function sourceLink(item,text,className) {
  const url=safeSource(item.url);
  const el=node(url?"a":"span",className,text);
  if(url){el.href=url;el.target="_blank";el.rel="noopener noreferrer";}
  return el;
}
function renderAnswer(container,response,language) {
  container.replaceChildren(); container.classList.remove("loading"); container.lang=language;
  const text=node("p","answer-text"); const answer=response.answer || "Не удалось получить ответ.";
  let cursor=0; const cited=new Set();
  for(const match of answer.matchAll(/\[(\d+)\]/g)) {
    text.append(document.createTextNode(answer.slice(cursor,match.index)));
    const index=Number(match[1])-1; const item=response.results[index];
    if(item){text.append(sourceLink(item,match[0],"citation"));cited.add(index);} else text.append(document.createTextNode(match[0]));
    cursor=match.index+match[0].length;
  }
  text.append(document.createTextNode(answer.slice(cursor))); container.append(text);
  if(response.notice) container.append(node("p","notice",response.notice));
  const sources=node("div","sources"); const photos=node("div","photos"); const seen=new Set(); let total=0;
  for(const index of cited) {
    const item=response.results[index]; const key=`${item.scope}/${item.document_id}`;
    if(seen.has(key)) continue; seen.add(key);
    sources.append(sourceLink(item,`[${index+1}] ${item.title} ↗`,"source-link"));
    for(const illustration of (item.images || []).slice(0,12)) {
      if(total>=12 || !illustration.url.startsWith("/api/web/images/")) break;
      const link=sourceLink(item,"","photo-link"); const image=node("img","");
      image.src=illustration.url; image.alt=illustration.alt || item.title; image.loading="lazy"; image.decoding="async";
      image.addEventListener("error",()=>{ link.remove(); if(!photos.children.length) photos.hidden=true; });
      link.append(image); photos.append(link); total++;
    }
  }
  if(sources.children.length) container.append(sources);
  if(photos.children.length){container.append(photos,node("p","photo-note","Фотографии из статьи. Нажмите, чтобы открыть оригинал в Битриксе."));}
}
async function submit(blob) {
  if(!blob.size) {status("Запись пустая. Повторите вопрос.");button();return;}
  state.busy=true; button("busy"); status("Распознаю речь и определяю язык…");
  let loading;
  try {
    const spoken=await api("/api/web/transcribe",{method:"POST",headers:{"Content-Type":blob.type || "audio/webm"},body:blob});
    currentLabel=labels[spoken.language] || labels["ru-RU"];
    document.body.classList.add("has-answers"); $("conversation").hidden=false;
    const question=node("article","question",spoken.query); question.lang=spoken.language;
    loading=node("article","answer loading","Ищу ответ в базе знаний…");
    $("conversation").append(question,loading); question.scrollIntoView({behavior:"smooth",block:"nearest"});
    status("Ищу инструкцию…");
    const response=await api("/api/web/search",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({query:spoken.query,language:spoken.language,history:state.history.slice(-8)})});
    renderAnswer(loading,response,spoken.language);
    state.history.push({role:"user",content:spoken.query},{role:"assistant",content:response.answer}); state.history=state.history.slice(-8);
    status("Ответ готов. Можно задать следующий вопрос.");
  } catch(error) {status(error.message); if(loading){loading.classList.remove("loading");loading.textContent=error.message;}}
  finally {state.busy=false;button();}
}
function cleanup() {
  clearTimeout(state.timer);clearInterval(state.interval);state.timer=null;state.interval=null;
  state.stream?.getTracks().forEach(track=>track.stop());state.stream=null;
  if(state.context){state.context.close().catch(()=>{});state.context=null;}
}
function stopRecording() {
  if(state.recorder?.state==="recording") {state.busy=true;button("busy");status("Отправляю запись…");state.recorder.stop();}
}
async function record() {
  if(state.recorder?.state==="recording"){stopRecording();return;}
  if(state.busy || !state.ready) return;
  state.busy=true;button("busy");status("Разрешите доступ к микрофону…");
  try {
    const stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true},video:false});
    state.stream=stream;
    const type=["audio/webm;codecs=opus","audio/mp4","audio/ogg;codecs=opus"].find(t=>MediaRecorder.isTypeSupported(t));
    const recorder=new MediaRecorder(stream,type?{mimeType:type}:{});state.recorder=recorder;const parts=[];
    recorder.ondataavailable=event=>{if(event.data.size) parts.push(event.data);};
    recorder.onstop=()=>{state.recorder=null;cleanup();void submit(new Blob(parts,{type:recorder.mimeType}));};
    recorder.onerror=()=>{recorder.onstop=null;cleanup();state.recorder=null;state.busy=false;button();status("Не удалось записать звук. Попробуйте ещё раз.");};
    recorder.start(250);state.busy=false;button("listening");status("Говорите. После паузы вопрос отправится автоматически.");
    state.timer=setTimeout(stopRecording,40000);
    const AudioContext=window.AudioContext || window.webkitAudioContext;
    try { if(AudioContext) {
      state.context=new AudioContext();void state.context.resume();
      const analyser=state.context.createAnalyser();analyser.fftSize=2048;
      state.context.createMediaStreamSource(stream).connect(analyser);
      const samples=new Float32Array(analyser.fftSize);let lastVoice=performance.now(),voiceTicks=0;
      state.interval=setInterval(()=>{
        analyser.getFloatTimeDomainData(samples);let energy=0;for(const value of samples)energy+=value*value;
        if(Math.sqrt(energy/samples.length)>.012){lastVoice=performance.now();voiceTicks++;}
        if(voiceTicks>=5 && performance.now()-lastVoice>1900)stopRecording();
      },100);
    }} catch { /* Recording still works; tap the same button to finish. */ }
  } catch(error) {cleanup();state.busy=false;button();status(error.name==="NotAllowedError"?"Разрешите микрофон в настройках браузера и нажмите кнопку снова.":"Не удалось включить микрофон. Проверьте его подключение.");}
}
$("voice-button").addEventListener("click",record);
window.addEventListener("pagehide",()=>{if(state.recorder){state.recorder.onstop=null;stopRecording();}cleanup();});
api("/api/web/status").then(result=>{
  state.ready=Boolean(result.configured && result.authenticated && result.voice_mode==="local_auto" && navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
  button();status(state.ready?"Нажмите кнопку и произнесите вопрос.":!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder?"Для голосовых вопросов откройте сайт в современном браузере с доступом к микрофону.":"Помощник пока недоступен. Попробуйте позже.");
}).catch(()=>status("Не удалось подключиться. Обновите страницу через несколько секунд."));
