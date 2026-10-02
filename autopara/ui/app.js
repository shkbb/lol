"use strict";

// ---------------------------------------------------------------------------
// Зв'язок із програмою
// ---------------------------------------------------------------------------

const TOKEN = (() => {
  const match = location.hash.match(/t=([\w-]+)/);
  let token = match ? match[1] : "";
  try {
    if (token) sessionStorage.setItem("autopara-token", token);
    else token = sessionStorage.getItem("autopara-token") || "";
  } catch (e) { /* без сховища — працюємо з токеном з адреси */ }
  if (match) history.replaceState(null, "", location.pathname);
  return token;
})();

async function api(method, path, body) {
  const options = { method, headers: { "X-Token": TOKEN } };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Помилка ${response.status}`);
  return data;
}

// ---------------------------------------------------------------------------
// Дрібниці
// ---------------------------------------------------------------------------

function h(tag, attrs, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith("on")) element.addEventListener(key.slice(2), value);
    else if (key === "class") element.className = value;
    else if (key in element && key !== "list") element[key] = value;
    else element.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === undefined || child === null || child === false) continue;
    element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return element;
}

let toastTimer;
function toast(text, bad = false) {
  const element = document.getElementById("toast");
  element.textContent = text;
  element.className = bad ? "toast bad" : "toast";
  element.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.hidden = true; }, bad ? 7000 : 3500);
}

async function busy(button, label, work) {
  const old = button.textContent;
  button.disabled = true;
  button.textContent = label;
  try { return await work(); }
  catch (e) { toast(e.message, true); }
  finally { button.disabled = false; button.textContent = old; }
}

const PLATFORM = { meet: "Meet", zoom: "Zoom", other: "сайт" };
const WEEKDAYS = ["Нд", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"];

function when(iso) {
  const date = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${WEEKDAYS[date.getDay()]} ${pad(date.getDate())}.${pad(date.getMonth() + 1)} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

// ---------------------------------------------------------------------------
// Налаштування й стан
// ---------------------------------------------------------------------------

let cfg = null;
let savedJson = "";
let meta = { universities: {}, profiles: [], path: "" };
let state = {};
let page = "home";
let lessonsCache = null;
let timetableCache = new Map();  // понеділок → дані тижня

function get(path) {
  return path.split(".").reduce((value, key) => (value == null ? undefined : value[key]), cfg);
}

function set(path, value) {
  const keys = path.split(".");
  let target = cfg;
  for (const key of keys.slice(0, -1)) target = target[key] ??= {};
  if (value === undefined) delete target[keys.at(-1)];
  else target[keys.at(-1)] = value;
  changed();
}

function changed() {
  document.getElementById("savebar").hidden = JSON.stringify(cfg) === savedJson;
}

async function loadConfig() {
  const data = await api("GET", "/api/config");
  meta = data;
  cfg = data.config;
  for (const section of ["schedule", "join", "me", "telegram", "ears", "brain"]) cfg[section] ??= {};
  cfg.links ??= [];
  savedJson = JSON.stringify(cfg);
  changed();
}

async function save() {
  const button = document.getElementById("save");
  await busy(button, "Зберігаю…", async () => {
    const result = await api("PUT", "/api/config", { config: cfg });
    cfg = result.config;
    savedJson = JSON.stringify(cfg);
    changed();
    lessonsCache = null;
    timetableCache = new Map();
    const messages = {
      applied: "Збережено й застосовано",
      saved: "Збережено",
      restarted: "Збережено, програму перезапущено",
      after_lesson: "Збережено — застосую, щойно скінчиться поточна пара",
      started: "Збережено — автоматичний вхід на пари увімкнено",
    };
    toast(messages[result.result] || "Збережено");
    render();
    refreshState();
  });
}

async function refreshState() {
  try {
    state = await api("GET", "/api/state");
  } catch (e) {
    state = { error: "Програма не відповідає — перезапусти autopara" };
  }
  const status = document.getElementById("brand-status");
  status.textContent = state.running ? "працює" : "вимкнено";
  if (page === "home") render();
}

// ---------------------------------------------------------------------------
// Поля форм
// ---------------------------------------------------------------------------

function field(label, hint, control) {
  return h("label", { class: "field" }, h("span", {}, label), control, hint && h("small", {}, hint));
}

function textField(path, label, hint, options = {}) {
  return field(label, hint, h("input", {
    type: options.type || "text",
    value: get(path) ?? "",
    placeholder: options.placeholder || "",
    autocomplete: "off",
    oninput: (event) => set(path, event.target.value),
  }));
}

function numberField(path, label, hint, options = {}) {
  return field(label, hint, h("input", {
    type: "number",
    min: options.min ?? 0,
    value: options.optional && !get(path) ? "" : get(path) ?? "",
    oninput: (event) => {
      const value = event.target.value.trim();
      set(path, value === "" ? (options.optional ? undefined : 0) : parseInt(value, 10));
    },
  }));
}

function checkField(path, label, hint, options = {}) {
  return h("label", { class: "check" },
    h("input", { type: "checkbox", checked: !!get(path), disabled: options.disabled, onchange: (event) => set(path, event.target.checked) }),
    h("div", {}, h("b", {}, label), hint && h("span", { class: "hint" }, hint)));
}

function choiceField(path, options) {
  const name = `choice-${path}`;
  return h("div", { class: "choices" }, options.map((option) => h("label", { class: "choice" },
    h("input", { type: "radio", name, checked: get(path) === option.value, onchange: () => set(path, option.value) }),
    h("div", {}, h("b", {}, option.label), option.hint && h("span", { class: "hint" }, option.hint)))));
}

function selectField(path, label, hint, options) {
  return field(label, hint, h("select", { onchange: (event) => set(path, event.target.value) },
    options.map(([value, text]) => h("option", { value, selected: (get(path) ?? "") === value }, text))));
}

function chipsField(path, label, hint, placeholder) {
  const list = () => get(path) || [];
  const wrap = h("div");
  const draw = () => {
    const input = h("input", {
      type: "text", placeholder,
      onkeydown: (event) => {
        if (event.key !== "Enter" || !input.value.trim()) return;
        event.preventDefault();
        set(path, [...list(), input.value.trim()]);
        draw();
        wrap.querySelector("input").focus();
      },
    });
    wrap.replaceChildren(
      h("div", { class: "chips" }, list().map((item, index) => h("span", { class: "chip" }, item,
        h("button", { class: "icon", title: "Прибрати", onclick: () => { set(path, list().filter((_, i) => i !== index)); draw(); } }, "✕")))),
      input,
    );
  };
  draw();
  return field(label, hint, wrap);
}

// ---------------------------------------------------------------------------
// Сторінки
// ---------------------------------------------------------------------------

const pages = {};

pages.home = () => {
  const content = [h("h1", {}, "Головна"), h("p", { class: "lead" }, "Програма сама заходить на пари за розкладом, з вимкненими мікрофоном і камерою.")];

  if (!state.configured) {
    content.push(h("div", { class: "card" },
      h("h2", {}, "Почнімо"),
      h("ol", { class: "steps" },
        h("li", {}, h("div", {}, h("b", {}, "Обери університет і групу"), h("div", { class: "muted" }, "Вкладка «Група»."))),
        h("li", {}, h("div", {}, h("b", {}, "Додай посилання на пари"), h("div", { class: "muted" }, "Вкладка «Посилання»: програма сама покаже предмети з твого розкладу."))),
        h("li", {}, h("div", {}, h("b", {}, "Увійди в Google"), h("div", { class: "muted" }, "Вкладка «Акаунти» → «Увійти в Google»."))),
        h("li", {}, h("div", {}, h("b", {}, "За бажанням — підключи Telegram"), h("div", { class: "muted" }, "Вкладка «Telegram»: свій бот за хвилину, сповіщення лише тобі."))),
      ),
      h("div", { class: "row" },
        h("button", { class: "primary", onclick: () => go("group") }, "Обрати групу"),
        importControls()),
    ));
  }

  const toggle = h("button", { class: state.running ? "ghost" : "primary" }, state.running ? "Вимкнути" : "Увімкнути");
  toggle.onclick = () => busy(toggle, "…", async () => {
    await api("POST", state.running ? "/api/stop" : "/api/start");
    await refreshState();
  });
  content.push(h("div", { class: "card" },
    h("div", { class: "status" },
      h("span", { class: state.running ? "dot on" : "dot" }),
      h("div", {},
        h("div", { class: "status-title" }, state.running ? "Працює" : "Вимкнено"),
        h("div", { class: "muted" }, [state.university, state.group].filter(Boolean).join(" · ") || "група не обрана")),
      h("span", { class: "spacer" }),
      toggle),
    state.restart_pending && h("div", { class: "banner warn", style: "margin:14px 0 0" }, "Нові налаштування застосуються, щойно скінчиться поточна пара."),
    state.error && h("div", { class: "banner bad", style: "margin:14px 0 0" }, state.error),
  ));

  if (state.active?.length) {
    content.push(h("div", { class: "card" }, h("h2", {}, "Зараз на парі"), h("div", { class: "list" }, state.active.map((session) => {
      const leave = h("button", { class: "ghost" }, "Вийти");
      leave.onclick = () => busy(leave, "…", async () => { await api("POST", "/api/leave", { key: session.key }); refreshState(); });
      return h("div", { class: "item" },
        h("span", { class: "badge ok" }, PLATFORM[session.platform] || session.platform),
        h("div", {}, h("div", { class: "title" }, session.title),
          session.participants && h("div", { class: "muted small" }, `👥 ${session.participants} учасників`)),
        h("span", { class: "spacer" }), leave);
    }))));
  }

  for (const conflict of state.conflicts || []) content.push(h("div", { class: "banner warn" }, conflict));

  const upcoming = state.upcoming || [];
  content.push(h("div", { class: "grid" },
    h("div", { class: "card" }, h("h2", {}, "Найближчі пари"),
      upcoming.length ? h("div", { class: "list" }, upcoming.map((visit) => h("div", { class: "item" },
        h("span", { class: "time" }, when(visit.join_at)),
        h("div", { class: "title" }, visit.title),
        h("span", { class: "spacer" }),
        h("span", { class: "badge accent" }, PLATFORM[visit.platform] || visit.platform))))
        : h("div", { class: "empty" }, state.running ? "Найближчими днями пар з посиланнями немає." : "Увімкни програму, щоб побачити план.")),
    h("div", { class: "card" }, h("h2", {}, "Без посилання"),
      state.unlinked?.length
        ? [h("div", { class: "list" }, state.unlinked.map((lesson) => h("div", { class: "item" },
            h("span", { class: "time" }, lesson.when), h("div", {}, lesson.title)))),
           h("button", { class: "soft", style: "margin-top:10px", onclick: () => go("links") }, "Додати посилання")]
        : h("div", { class: "empty" }, "Усі пари мають посилання 👍")),
  ));

  content.push(actionsCard());
  return content;
};

function importControls() {
  const fileInput = h("input", { type: "file", accept: ".toml", hidden: true, onchange: async (event) => {
    const file = event.target.files[0];
    if (!file) return;
    try {
      await api("POST", "/api/import", { toml: await file.text() });
      await loadConfig();
      toast("Налаштування імпортовано");
      refreshState();
    } catch (e) { toast(e.message, true); }
  } });
  return [
    ...meta.profiles.map((name) => {
      const button = h("button", { class: "soft" }, `Профіль «${name}»`);
      button.onclick = () => busy(button, "Імпортую…", async () => {
        await api("POST", "/api/import", { profile: name });
        await loadConfig();
        toast(`Профіль «${name}» імпортовано`);
        refreshState();
      });
      return button;
    }),
    h("button", { class: "ghost", onclick: () => fileInput.click() }, "Імпорт з файла…"),
    fileInput,
  ];
}

// Головна оновлюється кожні 5 с — введене й результати перевірки мають це пережити.
let joinDraft = "";
let lastChecks = [];

function checkRows(checks) {
  return checks.map((item) => h("div", { class: "item" },
    h("span", { class: item.ok ? "badge ok" : item.ok === null ? "badge" : "badge bad" }, item.ok ? "✓" : item.ok === null ? "·" : "✗"),
    h("b", {}, item.area), h("span", { class: "muted" }, item.text)));
}

function actionsCard() {
  const url = h("input", { type: "url", value: joinDraft, placeholder: "https://meet.google.com/xxx-xxxx-xxx",
    oninput: (event) => { joinDraft = event.target.value; } });
  const join = h("button", { class: "primary" }, "Зайти");
  join.onclick = () => busy(join, "Заходжу…", async () => {
    await api("POST", "/api/join", { url: url.value, minutes: 60 });
    toast("Заходжу — дивись вікно браузера");
    setTimeout(refreshState, 1500);
  });
  const login = h("button", { class: "ghost" }, "Увійти в Google");
  login.onclick = () => busy(login, "Відкриваю…", async () => {
    await api("POST", "/api/login");
    toast("Увійди в акаунти у вікні браузера програми");
  });
  const checks = h("div", { class: "list", style: "margin-top:10px" }, checkRows(lastChecks));
  const check = h("button", { class: "ghost" }, "Перевірити все");
  check.onclick = () => busy(check, "Перевіряю…", async () => {
    lastChecks = (await api("POST", "/api/check")).checks;
    checks.replaceChildren(...checkRows(lastChecks));
  });
  return h("div", { class: "card" },
    h("h2", {}, "Перевірка"),
    h("div", { class: "row" }, h("div", { style: "flex:1;min-width:240px" }, url), join),
    h("small", { class: "hint" }, "Тестовий вхід: створи зустріч на meet.new і встав посилання — програма зайде так само, як на пару."),
    h("div", { class: "row", style: "margin-top:12px" }, login, check),
    checks);
}

pages.group = () => {
  const university = get("schedule.university") || "duikt";
  const switcher = h("div", { class: "segmented" }, Object.entries(meta.universities).map(([code, name]) =>
    h("button", { class: code === university ? "on" : "", onclick: () => {
      if (code === university) return;
      set("schedule.university", code);
      for (const key of ["faculty_id", "course", "group_id", "group_uid", "student"]) set(`schedule.${key}`, undefined);
      set("schedule.group", "");
      render();
    } }, name)));

  const chosen = h("div", { class: "muted small", style: "margin-top:6px" });
  const describe = () => {
    const s = cfg.schedule;
    chosen.textContent = s.group
      ? `Обрано: ${s.group}${s.course ? ` · ${s.course} курс` : ""}${s.group_uid ? ` · id ${s.group_uid}` : ""}`
      : "Група не обрана";
  };
  describe();
  const results = h("div", { class: "results", hidden: true });
  let timer;
  const input = h("input", {
    type: "text", value: get("schedule.group") || "", autocomplete: "off",
    placeholder: university === "kpi" ? "ІП-21 або посилання schedule.kpi.ua/?groupId=…" : "ТЦР-32",
    oninput: () => {
      clearTimeout(timer);
      const text = input.value.trim();
      const link = text.match(/groupId=([\w-]+)/);
      if (link) {
        set("schedule.group", text);
        set("schedule.group_uid", link[1]);
        results.hidden = true;
        describe();
        return;
      }
      timer = setTimeout(() => search(text), 300);
    },
  });
  async function search(text) {
    if (text.length < 2) { results.hidden = true; return; }
    results.hidden = false;
    results.replaceChildren(h("div", { class: "empty", style: "padding:9px 12px" },
      university === "duikt" ? "Шукаю… (перший раз — кілька секунд: сайт віддає групи по факультетах)" : "Шукаю…"));
    try {
      const { groups } = await api("GET", `/api/groups?university=${university}&q=${encodeURIComponent(text)}`);
      results.replaceChildren(...(groups.length ? groups.map((group) => h("button", { onclick: () => {
        set("schedule.group", group.name);
        set("schedule.faculty_id", group.faculty_id ?? undefined);
        set("schedule.course", group.course ?? undefined);
        set("schedule.group_id", group.group_id ?? undefined);
        set("schedule.group_uid", group.uid || undefined);
        set("schedule.student", undefined);
        input.value = group.name;
        results.hidden = true;
        describe();
        render();
      } }, h("b", {}, group.name), h("span", { class: "muted" }, `  ${group.faculty || ""}${group.course ? ` · ${group.course} курс` : ""}`)))
        : [h("div", { class: "empty", style: "padding:9px 12px" }, "Нічого не знайшлося")]));
    } catch (e) {
      results.replaceChildren(h("div", { class: "banner bad", style: "margin:6px" }, e.message));
    }
  }

  const content = [
    h("h1", {}, "Група"),
    h("p", { class: "lead" }, "Звідки брати розклад і чий."),
    h("div", { class: "card" },
      h("h2", {}, "Університет і група"), switcher,
      h("label", { class: "field" }, h("span", {}, "Група"), h("div", { class: "search" }, input, results), chosen),
      university === "duikt" && studentControl(),
    ),
    h("div", { class: "card" },
      h("h2", {}, "Оновлення"),
      h("div", { class: "grid" },
        numberField("schedule.days_ahead", "На скільки днів уперед", "Скільки розкладу тримати в голові.", { min: 1 }),
        numberField("schedule.refresh_minutes", "Оновлювати кожні, хв", "Пари іноді переносять.", { min: 5 })),
      checkField("schedule.use_schedule_links", "Брати посилання з самого розкладу", "Якщо для пари немає твого посилання, а викладач вписав Meet чи Zoom у розклад."),
    ),
    h("div", { class: "card row" },
      h("div", { style: "flex:1" }, h("b", {}, "Перевір, що розклад правильний"),
        h("div", { class: "muted small" }, JSON.stringify(cfg) !== savedJson ? "Спершу збережи зміни." : "Пари на тиждень з позначками, куди програма заходитиме.")),
      h("button", { class: "soft", onclick: () => go("timetable") }, "Відкрити розклад")),
  ];
  return content;
};

function studentControl() {
  const s = cfg.schedule;
  const holder = h("div");
  const load = h("button", { class: "ghost", disabled: !s.group }, "Обрати зі списку групи");
  load.onclick = () => busy(load, "Завантажую…", async () => {
    const query = s.faculty_id && s.course && s.group_id
      ? `faculty_id=${s.faculty_id}&course=${s.course}&group_id=${s.group_id}`
      : `group=${encodeURIComponent(s.group)}`;
    const { students } = await api("GET", `/api/students?${query}`);
    holder.replaceChildren(h("select", { onchange: (event) => set("schedule.student", event.target.value || undefined) },
      h("option", { value: "" }, "— розклад усієї групи —"),
      students.map((name) => h("option", { value: name, selected: name === s.student }, name))));
  });
  holder.append(textField("schedule.student", "Студент (необов'язково)",
    "Тоді розклад береться саме твій — з урахуванням підгруп. Прізвище та ім'я, як на сайті розкладу.", { placeholder: "Прізвище Ім'я" }), load);
  return holder;
}

function lessonBadge(lesson) {
  if (lesson.source === "skip") return h("span", { class: "badge" }, "пропускаю");
  if (!lesson.url) return h("span", { class: "badge bad" }, "немає посилання");
  const text = `${PLATFORM[lesson.platform] || "сайт"}${lesson.source === "schedule" ? " · з розкладу" : ""}`;
  return h("span", { class: "badge ok", title: lesson.url }, text);
}

pages.links = () => {
  const content = [
    h("h1", {}, "Посилання"),
    h("p", { class: "lead" }, "Куди заходити на кожну пару. Програма бере предмети з твого розкладу — лишається вставити посилання."),
  ];
  const subjects = h("div", { class: "list" });
  const load = async () => {
    if (!lessonsCache) lessonsCache = (await api("GET", "/api/lessons?days=14")).lessons;
    const unique = new Map();
    for (const lesson of lessonsCache) unique.set(`${lesson.subject}|${lesson.type}`, lesson);
    subjects.replaceChildren(...(unique.size ? [...unique.values()].map(subjectRow) : [h("div", { class: "empty" }, "Найближчі два тижні пар немає.")]));
  };
  const reload = h("button", { class: "ghost" }, "Оновити");
  reload.onclick = () => busy(reload, "…", async () => { lessonsCache = null; await load(); });
  const subjectsCard = h("div", { class: "card" },
    h("div", { class: "row" }, h("h2", { style: "margin:0" }, "Пари з розкладу (2 тижні)"), h("span", { class: "spacer" }), reload),
    JSON.stringify(cfg) !== savedJson && h("small", { class: "hint" }, "Статуси показані для збережених налаштувань."),
    subjects);
  if (get("schedule.group") || get("schedule.group_uid")) {
    subjects.replaceChildren(h("div", { class: "empty" }, "Завантажую пари…"));
    load().catch((e) => subjects.replaceChildren(h("div", { class: "banner bad" }, e.message)));
  } else {
    subjects.replaceChildren(h("div", { class: "empty" }, "Спершу обери групу у вкладці «Група»."));
  }
  content.push(subjectsCard, rulesCard());
  return content;
};

function accountOptions(current) {
  const accounts = [...new Set([...(cfg.join.accounts || []), current].filter(Boolean))];
  return [["", `основний${cfg.join.meet_account ? ` (${cfg.join.meet_account})` : ""}`], ...accounts.map((a) => [a, a])];
}

function subjectRow(lesson) {
  const editor = h("div", { hidden: true, style: "width:100%;margin-top:10px" });
  const url = h("input", { type: "url", placeholder: "https://meet.google.com/… або https://…zoom.us/j/…" });
  const account = h("select", {}, accountOptions("").map(([value, text]) => h("option", { value }, text)));
  const onlyType = h("input", { type: "checkbox" });
  const skip = h("input", { type: "checkbox", onchange: () => { url.disabled = skip.checked; } });
  editor.append(
    h("div", { class: "row" }, h("div", { style: "flex:2;min-width:240px" }, url), h("div", { style: "flex:1;min-width:160px" }, account)),
    h("div", { class: "row", style: "margin-top:8px" },
      lesson.type && h("label", { class: "check", style: "margin:0" }, onlyType, h("span", {}, `лише для «${lesson.type}»`)),
      h("label", { class: "check", style: "margin:0" }, skip, h("span", {}, "не заходити на цю пару")),
      h("span", { class: "spacer" }),
      h("button", { class: "primary", onclick: () => {
        if (!skip.checked && !url.value.trim()) { toast("Встав посилання або постав «не заходити»", true); return; }
        const rule = { subject: lesson.subject };
        if (onlyType.checked) rule.type = lesson.type;
        if (skip.checked) rule.skip = true; else rule.url = url.value.trim();
        if (account.value) rule.account = account.value;
        cfg.links.unshift(rule);
        changed();
        render();
        toast("Правило додано — збережи зміни");
      } }, "Додати")));
  return h("div", { class: "item", style: "flex-wrap:wrap" },
    h("div", { style: "flex:1;min-width:220px" },
      h("div", { class: "title" }, lesson.subject, lesson.type && h("span", { class: "muted" }, ` (${lesson.type})`)),
      h("div", { class: "muted small" }, lesson.teacher || "")),
    lessonBadge(lesson),
    h("button", { class: "ghost", onclick: () => { editor.hidden = !editor.hidden; } }, lesson.url ? "Змінити" : "Задати"),
    editor);
}

function rulesCard() {
  const rows = cfg.links.map((rule, index) => {
    const bind = (key, placeholder, cls) => h("input", { type: "text", class: cls, value: rule[key] || "", placeholder,
      oninput: (event) => { if (event.target.value) rule[key] = event.target.value; else delete rule[key]; changed(); } });
    const move = (delta) => {
      const target = index + delta;
      if (target < 0 || target >= cfg.links.length) return;
      [cfg.links[index], cfg.links[target]] = [cfg.links[target], cfg.links[index]];
      changed();
      render();
    };
    return h("tr", {},
      h("td", {}, bind("subject", "предмет (варіанти через |)")),
      h("td", { class: "narrow" }, bind("type", "тип")),
      h("td", {}, rule.skip ? h("span", { class: "muted" }, "не заходити") : bind("url", "посилання")),
      h("td", {}, h("select", { onchange: (event) => { if (event.target.value) rule.account = event.target.value; else delete rule.account; changed(); } },
        accountOptions(rule.account).map(([value, text]) => h("option", { value, selected: (rule.account || "") === value }, text)))),
      h("td", {}, h("input", { type: "checkbox", title: "Не заходити", checked: !!rule.skip,
        onchange: (event) => { if (event.target.checked) rule.skip = true; else delete rule.skip; changed(); render(); } })),
      h("td", { style: "white-space:nowrap" },
        h("button", { class: "icon", title: "Вище", onclick: () => move(-1) }, "↑"),
        h("button", { class: "icon", title: "Нижче", onclick: () => move(1) }, "↓"),
        h("button", { class: "icon", title: "Видалити", onclick: () => { cfg.links.splice(index, 1); changed(); render(); } }, "✕")));
  });
  return h("div", { class: "card" },
    h("h2", {}, "Правила"),
    h("small", { class: "hint", style: "margin:-6px 0 12px" },
      "Перевіряються згори донизу; перше, що підійшло, і спрацьовує. «Предмет» — шматок назви (регістр не важливий), кілька варіантів через «|». Тип — Лк, Пз чи Лб, якщо посилання різні."),
    cfg.links.length
      ? h("table", { class: "rules" }, h("tr", {}, ["Предмет", "Тип", "Посилання", "Акаунт", "Пропуск", ""].map((name) => h("th", {}, name))), rows)
      : h("div", { class: "empty" }, "Правил ще немає — додай їх зі списку пар вище або вручну."),
    h("button", { class: "ghost", style: "margin-top:10px", onclick: () => { cfg.links.push({ subject: "", url: "" }); changed(); render(); } }, "Додати правило"));
}

pages.accounts = () => {
  const login = h("button", { class: "primary" }, "Увійти в Google");
  login.onclick = () => busy(login, "Відкриваю…", async () => {
    await api("POST", "/api/login");
    toast("Увійди в акаунти у вікні браузера програми");
  });
  return [
    h("h1", {}, "Акаунти"),
    h("p", { class: "lead" }, "З яких акаунтів заходити на пари."),
    h("div", { class: "card" },
      h("h2", {}, "Google Meet"),
      textField("join.meet_account", "Основний акаунт", "З нього програма заходить на всі зустрічі Meet. Найчастіше — університетська пошта.", { placeholder: "st0000000@stud.example.edu.ua" }),
      chipsField("join.accounts", "Інші акаунти", "Для зустрічей, які не пускають з основного (наприклад, особиста пошта). Акаунт для пари обирається у вкладці «Посилання».", "email і Enter"),
      h("div", { class: "row" }, login,
        h("span", { class: "muted small" }, "Відкриється браузер програми: увійди в усі ці акаунти (наступний — через «Додати обліковий запис»)."))),
    h("div", { class: "card" },
      h("h2", {}, "Zoom"),
      choiceField("join.zoom_mode", [
        { value: "app", label: "Zoom-клієнт", hint: "Надійніше. Мікрофон і камеру вимикають налаштування самого Zoom (вимкнення при вході)." },
        { value: "browser", label: "Zoom у браузері", hint: "Експериментально, але працює розумний вихід і помічник." },
      ]),
      textField("join.display_name", "Ім'я для входу гостем", "Якщо Zoom чи Meet пустять лише як гостя.", { placeholder: "Прізвище Ім'я Група" })),
    h("div", { class: "card" },
      h("h2", {}, "Браузер"),
      selectField("join.browser_channel", "Який браузер відкривати", "Логін у Google зберігається в окремому профілі програми.", [["chrome", "Google Chrome"], ["msedge", "Microsoft Edge"], ["", "вбудований Chromium"]]),
      textField("join.browser_path", "Або шлях до браузера", "Наприклад, /usr/bin/chromium. Має пріоритет над вибором вище.", { placeholder: "необов'язково" })),
  ];
};

// --- Розклад ------------------------------------------------------------------

const DAY_NAMES = ["Неділя", "Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота"];
let weekStart = null;  // понеділок тижня, що показуємо (РРРР-ММ-ДД)

function isoDate(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function addDays(iso, days) {
  const date = new Date(`${iso}T12:00:00`);
  date.setDate(date.getDate() + days);
  return isoDate(date);
}

function thisWeek() {
  const today = new Date();
  const shift = today.getDay() === 0 ? 1 : 1 - today.getDay();  // у неділю — вже наступний тиждень
  today.setDate(today.getDate() + shift);
  return isoDate(today);
}

function shortDate(iso) {
  return `${iso.slice(8, 10)}.${iso.slice(5, 7)}`;
}

function minutesOf(hhmm) {
  const [hours, minutes] = hhmm.split(":").map(Number);
  return hours * 60 + minutes;
}

function lessonItem(lesson, data) {
  const today = lesson.date === data.today;
  const now = today ? minutesOf(data.now) : null;
  const past = lesson.date < data.today || (today && now >= minutesOf(lesson.end));
  const current = today && now >= minutesOf(lesson.start) && now < minutesOf(lesson.end);
  let join = null;
  if (today && !past && lesson.url) {
    join = h("button", { class: current ? "primary" : "ghost" }, "Зайти зараз");
    join.onclick = () => busy(join, "…", async () => {
      const minutes = Math.max(minutesOf(lesson.end) - now, 0) + 10;
      await api("POST", "/api/join", { url: lesson.url, minutes });
      toast(`Заходжу на «${lesson.subject}» — стежити можна на головній`);
    });
  }
  return h("div", { class: `lesson${past ? " past" : ""}${current ? " now" : ""}` },
    h("div", { class: "row" },
      h("span", { class: "time" }, `${lesson.start}–${lesson.end}`),
      current && h("span", { class: "badge accent" }, "зараз"),
      h("span", { class: "spacer" }),
      lessonBadge(lesson)),
    h("div", { class: "title" }, lesson.subject, lesson.type && h("span", { class: "muted" }, ` (${lesson.type})`)),
    h("div", { class: "muted small" }, [lesson.teacher, lesson.room].filter(Boolean).join(" · ")),
    join && h("div", { style: "margin-top:8px" }, join));
}

pages.timetable = () => {
  const start = weekStart || thisWeek();
  const body = h("div", {}, h("div", { class: "empty" }, "Завантажую розклад…"));
  const label = h("span", { class: "week-label" }, `${shortDate(start)} – ${shortDate(addDays(start, 6))}`);
  const move = (days) => { weekStart = addDays(start, days); render(); };
  const refresh = h("button", { class: "ghost" }, "Оновити");
  refresh.onclick = () => busy(refresh, "…", async () => { timetableCache.delete(start); await load(); });
  const header = h("div", { class: "row", style: "margin-bottom:16px" },
    h("button", { class: "ghost", onclick: () => move(-7) }, "←"),
    label,
    h("button", { class: "ghost", onclick: () => move(7) }, "→"),
    start !== thisWeek() && h("button", { class: "soft", onclick: () => { weekStart = null; render(); } }, "Цей тиждень"),
    h("span", { class: "spacer" }),
    refresh);

  async function load() {
    let data = timetableCache.get(start);
    if (!data) {
      data = await api("GET", `/api/lessons?start=${start}&days=6`);
      timetableCache.set(start, data);
    }
    if (page !== "timetable" || (weekStart || thisWeek()) !== start) return;  // вже гортнули далі
    const byDay = new Map();
    for (const lesson of data.lessons) {
      if (!byDay.has(lesson.date)) byDay.set(lesson.date, []);
      byDay.get(lesson.date).push(lesson);
    }
    const days = [];
    for (let i = 0; i < 7; i++) {
      const iso = addDays(start, i);
      const lessons = byDay.get(iso) || [];
      if (i === 6 && !lessons.length) continue;  // неділя без пар
      const weekday = DAY_NAMES[new Date(`${iso}T12:00:00`).getDay()];
      days.push(h("div", { class: `card${iso === data.today ? " today" : ""}` },
        h("h2", {}, `${weekday}, ${shortDate(iso)}`, iso === data.today && h("span", { class: "badge accent" }, "сьогодні")),
        lessons.length ? h("div", {}, lessons.map((lesson) => lessonItem(lesson, data)))
          : h("div", { class: "empty" }, "Пар немає")));
    }
    const missing = data.lessons.filter((lesson) => !lesson.url && lesson.source !== "skip").length;
    body.replaceChildren(
      h("p", { class: "muted", style: "margin:-8px 0 14px" }, data.group,
        missing ? ` · без посилання: ${missing} — додай у вкладці «Посилання»` : data.lessons.length ? " · усі пари з посиланнями" : ""),
      h("div", { class: "days" }, days));
  }

  const content = [h("h1", {}, "Розклад"), h("p", { class: "lead" }, "Твої пари і куди програма заходитиме.")];
  if (!get("schedule.group") && !get("schedule.group_uid")) {
    content.push(h("div", { class: "card" }, h("p", {}, "Спершу обери групу."),
      h("button", { class: "primary", onclick: () => go("group") }, "Обрати групу")));
    return content;
  }
  load().catch((e) => body.replaceChildren(h("div", { class: "banner bad" }, e.message)));
  content.push(header, body);
  return content;
};

// --- Помічник -----------------------------------------------------------------

const EARS_GB = { small: 1, medium: 1.5, "large-v3-turbo": 2 };

function brainGb(model) {
  const size = /(\d+(?:\.\d+)?)b\b/i.exec(model || "");
  return size ? Math.round((parseFloat(size[1]) * 0.7 + 1) * 10) / 10 : null;
}

function resourcesCard() {
  const gb = (value) => `≈ ${String(value).replace(".", ",")} ГБ`;
  const rows = [["Браузер на парі (Meet)", 1.2, "процесор 10–25 %"], ["Сама програма", 0.15, "майже 0 %"]];
  if (cfg.ears.enabled) {
    const model = cfg.ears.model || "small";
    rows.push([`Вуха (${model})`, EARS_GB[model] ?? 1.5, "30–50 %, поки на парі говорять"]);
  }
  let unknown = false;
  if (cfg.brain.enabled) {
    const size = brainGb(cfg.brain.model);
    unknown = size === null;
    rows.push([`Мозок (${cfg.brain.model || "gemma3:4b"})`, size, "100 % на 10–30 с, коли щось сталося"]);
  }
  const total = rows.reduce((sum, row) => sum + (row[1] || 0), 0);
  return h("div", { class: "card" },
    h("h2", {}, "Скільки це їсть"),
    h("table", { class: "estimate" },
      rows.map(([name, size, cpu]) => h("tr", {}, h("td", {}, name, h("div", { class: "muted small" }, cpu)),
        h("td", {}, size === null ? "залежить від моделі" : gb(size)))),
      h("tr", { class: "total" }, h("td", {}, "Разом під час пари"), h("td", {}, `${gb(Math.round(total * 10) / 10)}${unknown ? " + мозок" : ""}`))),
    h("small", { class: "hint" },
      "Оцінка пам'яті для ноутбука на кшталт i5 11-го покоління з 16 ГБ. Між парами програма майже нічого не їсть, ",
      "а Ollama сама вивантажує модель через 5 хв простою. Диск: whisper small — 0,5 ГБ, large-v3-turbo — 1,6 ГБ, gemma3:4b — 3,3 ГБ. ",
      "На батареї з вухами й мозком ноутбук сідає помітно швидше — краще бути на зарядці."));
}

pages.assistant = () => {
  const linux = (meta.platform || "").startsWith("linux");
  const check = h("button", { class: "ghost" }, "Перевірити мозок і вуха");
  const checks = h("div", { class: "list" });
  check.onclick = () => busy(check, "Перевіряю…", async () => {
    const result = await api("POST", "/api/check");
    checks.replaceChildren(...result.checks.filter((item) => ["мозок", "вуха", "відеовирізки"].includes(item.area)).map((item) =>
      h("div", { class: "item" }, h("span", { class: item.ok ? "badge ok" : item.ok === null ? "badge" : "badge bad" }, item.area), h("span", {}, item.text))));
  });
  return [
    h("h1", {}, "Помічник"),
    h("p", { class: "lead" }, "Стежить за парою, поки тебе немає: відмічається на перекличці, кличе тебе, коли звертаються, шле звіт після пари."),
    h("div", { class: "card" },
      h("h2", {}, "Про тебе"),
      textField("me.name", "Прізвище та ім'я", "Як тебе називають викладачі — щоб помічати, коли кличуть.", { placeholder: "Прізвище Ім'я" }),
      h("div", { class: "grid" },
        textField("me.present", "Що писати на перекличці", "", { placeholder: "Присутній" }),
        textField("me.here", "Відповідь на «…, ви тут?»", "", { placeholder: "Так, я тут" })),
      chipsField("me.quick_replies", "Кнопки-відповіді в Telegram", "Коли тебе кличуть, під сповіщенням будуть ці кнопки: натиснув — і текст пішов у чат пари.", "текст і Enter")),
    h("div", { class: "card" },
      h("h2", {}, "Писати в чат пари самому"),
      choiceField("me.chat_reply", [
        { value: "auto", label: "Так, де сигнали надійні", hint: "Коли не всі сигнали за — спитає в Telegram, а за 45 с без твого «ні» напише сам." },
        { value: "ask", label: "Лише після мого «так» у Telegram", hint: "Кожен раз питає кнопками." },
        { value: "off", label: "Ніколи", hint: "Лише сповіщає." },
      ]),
      h("div", { style: "margin:4px 0 8px;font-weight:600" }, "Що саме можна писати"),
      checkField("me.roll_call", "Відмічатися на перекличці", "«+», «Присутній» чи прізвище, коли відмічається група. Вимкнено — лише питає тебе."),
      checkField("me.answer_here", "Відповідати, коли викладач питає саме тебе", "«Ляшко, ви тут?» → «Так, я тут». Вимкнено — лише питає."),
      checkField("me.polite", "Дублювати за групою «Дякую», «Добрий день», «До побачення»", "Вимкнено — мовчить."),
      h("small", { class: "hint" }, "На запис, голосування, «хто здав» чи «зрозуміло?» помічник сам не відповідає ніколи — це вирішуєш ти.")),
    h("div", { class: "card" },
      h("h2", {}, "Вуха й мозок"),
      checkField("ears.enabled", "Слухати пару",
        "Розпізнає мову локально, без інтернету: кличуть тебе, перекличка голосом, розшифровка для звіту. Потрібен пакет autopara[ears]."),
      selectField("ears.model", "Модель розпізнавання", "large-v3-turbo — точніша для української; якщо комп'ютер не встигатиме, програма сама перейде на small.",
        [["small", "small — швидка"], ["medium", "medium"], ["large-v3-turbo", "large-v3-turbo — точна"]]),
      linux && selectField("ears.source", "Звідки брати звук", "Зі сторінки пари — на будь-якій ОС і навіть із вимкненим звуком; системний — через PipeWire/PulseAudio, чує все, що грає браузер.",
        [["auto", "автоматично (системний, якщо є)"], ["browser", "зі сторінки пари"], ["pulse", "системний звук Linux"]]),
      checkField("brain.enabled", "Мозок (локальна мовна модель)", "Через Ollama (є для Linux, Windows і macOS). Розбирається в незапрограмованих ситуаціях і складає звіт після пари."),
      h("div", { class: "grid" },
        textField("brain.model", "Модель", "", { placeholder: "gemma3:4b" }),
        textField("brain.url", "Адреса Ollama", "", { placeholder: "http://127.0.0.1:11434" })),
      check, checks),
    resourcesCard(),
  ];
};

// --- Telegram -----------------------------------------------------------------

let botInfo = { token: "", username: "", error: "" };
let pairing = null;  // { code } — поки чекаємо, що ти натиснеш Start

async function checkBot(token) {
  try {
    const result = await api("POST", "/api/telegram/check", { token });
    botInfo = { token, username: result.username, error: "" };
  } catch (e) {
    botInfo = { token, username: "", error: e.message };
  }
}

function pairCode() {
  const random = new Uint32Array(1);
  crypto.getRandomValues(random);
  return String(random[0] % 1000000).padStart(6, "0");
}

pages.telegram = () => {
  const token = (get("telegram.token") || "").trim();
  const chatId = get("telegram.chat_id") || 0;
  const connected = chatId !== 0;
  const known = token && botInfo.token === token;
  const username = known ? botInfo.username : "";
  if (token && !known) checkBot(token).then(() => { if (page === "telegram") render(); });

  const tokenInput = h("input", { type: "password", value: token, placeholder: "123456789:AAH…", autocomplete: "off",
    oninput: (event) => { set("telegram.token", event.target.value.trim()); set("telegram.chat_id", 0); } });
  const checkButton = h("button", { class: "soft" }, "Перевірити");
  checkButton.onclick = () => busy(checkButton, "…", async () => {
    await checkBot((get("telegram.token") || "").trim());
    render();
  });

  const connect = h("button", { class: "primary", disabled: !username }, connected ? "Підключити заново" : "Підключити");
  connect.onclick = async () => {
    pairing = { code: pairCode() };
    render();
    window.open(`https://t.me/${username}?start=${pairing.code}`, "_blank", "noopener");
    try {
      const result = await api("POST", "/api/telegram/pair", { token, code: pairing.code });
      pairing = null;
      if (!result.chat_id) throw new Error("Не дочекався — натисни «Підключити» ще раз");
      set("telegram.chat_id", result.chat_id);
      await save();
      toast("Підключено! Бот уже привітався в Telegram");
    } catch (e) {
      pairing = null;
      toast(e.message, true);
      render();
    }
  };
  const test = h("button", { class: "ghost" }, "Надіслати тест");
  test.onclick = () => busy(test, "…", async () => {
    await api("POST", "/api/telegram/test", { token, chat_id: chatId });
    toast("Надіслано — перевір Telegram");
  });

  const botLink = (text) => h("a", { href: `https://t.me/${username}`, target: "_blank", rel: "noopener" }, text);
  const steps = h("ol", { class: "steps" },
    h("li", {}, h("div", {},
      h("b", {}, "Створи свого бота"),
      h("div", { class: "muted" }, "Відкрий ", h("a", { href: "https://t.me/BotFather", target: "_blank", rel: "noopener" }, "@BotFather"),
        " → «Start» → надішли /newbot. Назва — будь-яка, ім'я — латиницею й закінчується на «bot» (наприклад, ivan_autopara_bot). ",
        "У відповідь прийде токен: довгий рядок на кшталт 123456789:AAH…"))),
    h("li", {}, h("div", { style: "flex:1" },
      h("b", {}, "Встав токен"),
      h("div", { class: "row", style: "margin-top:6px" }, h("div", { style: "flex:1;min-width:240px" }, tokenInput), checkButton),
      known && username && h("div", { style: "margin-top:8px" }, h("span", { class: "badge ok" }, "✓ бот знайдено"), " ", botLink(`@${username}`)),
      known && botInfo.error && h("div", { class: "banner bad", style: "margin:8px 0 0" }, botInfo.error))),
    h("li", {}, h("div", { style: "flex:1" },
      h("b", {}, "Підключи свій Telegram"),
      h("div", { class: "muted" }, "Відкриється твій бот — натисни в ньому «Start». Сповіщення отримуватиме лише той, хто це зробить."),
      pairing
        ? h("div", { class: "waiting" },
            h("div", {}, "Чекаю, поки ти натиснеш «Start» у ", botLink(`@${username}`), " (до 3 хвилин)."),
            h("div", { class: "muted small", style: "margin-top:6px" }, "Telegram на телефоні? Знайди там бота й надішли йому код:"),
            h("div", { class: "code" }, pairing.code))
        : h("div", { class: "row", style: "margin-top:8px" }, connect, connected && test,
            connected && h("span", { class: "badge ok" }, "✓ підключено")))));

  return [
    h("h1", {}, "Telegram"),
    h("p", { class: "lead" }, "Твій особистий бот: пише, коли тебе кличуть, питає «написати «+»?» кнопками, шле звіт після пари. ",
      "Токен лишається лише на цьому комп'ютері, тож твоїх сповіщень ніхто інший не бачить."),
    h("div", { class: "card" }, steps),
    h("div", { class: "card" },
      h("h2", {}, "Що бот робить"),
      checkField("telegram.commands", "Відповідати на команди й кнопки",
        "«статус», «чат», «екран», «напиши …», «вийди», кнопки «Написати «+»?». Вимкни — і бот лише надсилатиме сповіщення, нічого не відповідаючи й нічого не роблячи за твоїми повідомленнями."),
      checkField("telegram.mentions", "Сповіщати, коли тебе кличуть", "З цитатою й кнопками-відповідями."),
      checkField("telegram.clips", "Відеовирізка моменту", "Хвилина відео з чатом (і звуком, якщо ввімкнені вуха), коли тебе згадали. Потрібен ffmpeg."),
      checkField("telegram.reports", "Звіт після пари", "Про що була пара, завдання й дедлайни, чи тебе кликали. З мозком — з підсумком."),
      checkField("telegram.joins", "Повідомляти, коли зайшов чи вийшов з пари", "Якщо зайти не вдалося — повідомить завжди."),
      checkField("telegram.desktop", "Дублювати сповіщення на робочий стіл", "Звичайні сповіщення системи — Windows, macOS чи Linux.")),
  ];
};

// --- Запуск -------------------------------------------------------------------

function autostartCard() {
  const auto = meta.autostart || { supported: false, enabled: false, note: "" };
  const box = h("input", { type: "checkbox", checked: !!auto.enabled, disabled: !auto.supported });
  box.onchange = async () => {
    try {
      meta.autostart = await api("POST", "/api/autostart", { enabled: box.checked });
      toast(meta.autostart.enabled ? "Запускатиметься разом із комп'ютером" : "Автозапуск вимкнено");
    } catch (e) {
      box.checked = !box.checked;
      toast(e.message, true);
    }
  };
  return h("div", { class: "card" },
    h("h2", {}, "Запуск"),
    h("label", { class: "check" }, box, h("div", {},
      h("b", {}, "Запускати разом із комп'ютером"),
      h("span", { class: "hint" }, auto.note || "У фоні, без вікна. Вікно — ярликом або командою autopara app."))));
}

pages.behaviour = () => [
  h("h1", {}, "Поведінка"),
  h("p", { class: "lead" }, "Коли запускатися, заходити й виходити."),
  autostartCard(),
  h("div", { class: "card" },
    h("h2", {}, "Вхід"),
    numberField("join.minutes_before", "За скільки хвилин до пари заходити", ""),
    checkField("join.mute_microphone", "Вимикати мікрофон", "Якщо не вдасться переконатися, що він вимкнений, браузер заборонить сторінці доступ до нього."),
    checkField("join.camera_off", "Вимикати камеру", ""),
    checkField("join.mute_sound", "Без звуку", "Вимкни, щоб чути пару з колонок (помічник однаково чутиме).")),
  h("div", { class: "card" },
    h("h2", {}, "Вихід"),
    checkField("join.auto_leave", "Виходити з пари самому", ""),
    checkField("join.smart_leave", "Разом з групою, а не рівно за розкладом", "Відпустили раніше — вийде, коли вийде більшість; затягнулась — не вийде першим."),
    h("div", { class: "grid" },
      numberField("join.max_overtime_minutes", "Не довше ніж хв після кінця", ""),
      numberField("join.leave_after_minutes", "Якщо учасників не видно — через хв після кінця", ""))),
];

let logAfter = 0;
let logTimer;
pages.log = () => {
  const box = h("div", { class: "log" });
  logAfter = 0;
  const pull = async () => {
    if (page !== "log") return;
    try {
      const { lines } = await api("GET", `/api/log?after=${logAfter}`);
      const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
      for (const line of lines) {
        logAfter = line.n;
        box.append(h("div", { class: line.level }, line.text));
      }
      if (stick) box.scrollTop = box.scrollHeight;
    } catch (e) { /* програма перезапускається */ }
    logTimer = setTimeout(pull, 3000);
  };
  clearTimeout(logTimer);
  pull();
  return [h("h1", {}, "Журнал"), h("p", { class: "lead" }, `Що програма робила. Налаштування: ${meta.path}`), box];
};

// ---------------------------------------------------------------------------
// Навігація
// ---------------------------------------------------------------------------

function render() {
  const main = document.getElementById("main");
  if (!cfg) return;
  const focused = document.activeElement;
  if (page === "home" && focused && main.contains(focused) && focused.tagName === "INPUT") return;  // не збивати введення
  main.replaceChildren(...[pages[page]()].flat());
}

function go(name) {
  page = name;
  for (const button of document.querySelectorAll("#nav button")) button.classList.toggle("active", button.dataset.page === name);
  try { localStorage.setItem("autopara-page", name); } catch (e) { /* не страшно */ }
  render();
  window.scrollTo(0, 0);
}

document.getElementById("nav").addEventListener("click", (event) => {
  if (event.target.dataset.page) go(event.target.dataset.page);
});
document.getElementById("save").addEventListener("click", save);
document.getElementById("discard").addEventListener("click", () => { cfg = JSON.parse(savedJson); changed(); render(); });
window.addEventListener("beforeunload", (event) => {
  if (cfg && JSON.stringify(cfg) !== savedJson) event.preventDefault();
});

(async function start() {
  if (!TOKEN) {
    document.getElementById("main").append(h("div", { class: "banner bad" }, "Відкрий вікно командою autopara app."));
    return;
  }
  try {
    await loadConfig();
  } catch (e) {
    document.getElementById("main").append(h("div", { class: "banner bad" }, e.message));
    return;
  }
  try { page = localStorage.getItem("autopara-page") || "home"; } catch (e) { page = "home"; }
  if (!pages[page]) page = "home";
  go(page);
  await refreshState();
  setInterval(refreshState, 5000);
})();
