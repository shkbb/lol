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
  for (const section of ["schedule", "join", "me", "telegram", "ears", "brain", "study", "agent"]) cfg[section] ??= {};
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
    if (result.autostart) {
      meta.autostart = { ...(meta.autostart || {}), enabled: true };
      setTimeout(() => toast("Програма тепер запускатиметься разом із комп'ютером (вимкнути — «Поведінка»)"), 3600);
    }
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
        { value: "auto", label: "Автоматично (рекомендовано)", hint: "Zoom-клієнт, якщо вуха можуть його чути (Linux або віртуальний кабель); інакше Zoom у браузері, а якщо там не пустять — таки клієнт, і це посилання програма запам'ятає." },
        { value: "app", label: "Завжди Zoom-клієнт", hint: "Мікрофон і камеру вимикають налаштування самого Zoom (вимкнення при вході)." },
        { value: "browser", label: "Завжди Zoom у браузері", hint: "Працюють розумний вихід і кадри у вирізках; деякі викладачі вхід через браузер вимикають." },
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

function earsInstallBanner() {
  const install = h("button", { class: "primary" }, "Встановити розпізнавання мови");
  install.onclick = () => busy(install, "Встановлюю… (1–3 хв)", async () => {
    await api("POST", "/api/install-ears");
    meta.ears_installed = true;
    render();
    toast("Готово — вуха запрацюють, щойно завантажиться модель (перший раз кілька хвилин)");
  });
  return h("div", { class: "banner warn", style: "display:flex;gap:12px;align-items:center;flex-wrap:wrap" },
    h("span", { style: "flex:1;min-width:220px" }, "Для вух потрібен ще один пакет (~200 МБ). Програма встановить його сама."), install);
}

let audioDevices = null;  // пристрої запису (для віртуального кабелю Zoom), завантажуються раз

function zoomEars(linux) {
  const mode = get("join.zoom_mode") || "auto";
  if (mode === "browser") {
    return h("small", { class: "hint" }, "Zoom відкривається в браузері — вуха чують його так само, як Meet.");
  }
  if (linux) {
    return h("small", { class: "hint" }, "Zoom-клієнт: на час пари програма слухає лише його звук (PipeWire/PulseAudio) — налаштовувати нічого не треба.");
  }
  if (audioDevices === null) {
    audioDevices = [];
    api("GET", "/api/audio-devices").then((result) => {
      audioDevices = result.devices;
      if (page === "assistant") render();
    }).catch(() => {});
  }
  const current = get("ears.app_device") || "";
  const names = [...new Set([...audioDevices, current].filter(Boolean))];
  const select = h("select", { onchange: (event) => set("ears.app_device", event.target.value || undefined) },
    h("option", { value: "" }, "автоматично (знайду встановлений кабель сам)"),
    names.map((name) => h("option", { value: name, selected: name === current }, name)));
  const mac = (meta.platform || "") === "darwin";
  const cable = mac
    ? h("a", { href: "https://existential.audio/blackhole/", target: "_blank", rel: "noopener" }, "BlackHole")
    : h("a", { href: "https://vb-audio.com/Cable/", target: "_blank", rel: "noopener" }, "VB-Cable");
  return h("div", { class: "card", style: "background:var(--panel-2);box-shadow:none;margin:6px 0 14px" },
    h("b", {}, "Zoom-клієнт"),
    h("div", { class: "muted small", style: "margin-bottom:8px" },
      mode === "auto"
        ? "Поки віртуального кабелю немає, Zoom відкриватиметься в браузері — так вуха його чують. Щоб і в Zoom-клієнті:"
        : "Щоб вуха чули пари в Zoom-клієнті:"),
    h("div", { class: "small", style: "margin-bottom:8px" }, "встанови ", cable,
      mac ? ", у Zoom → Налаштування → Аудіо → Динамік обери BlackHole (або Multi-Output з ним)." : ", у Zoom → Налаштування → Аудіо → Динамік обери «CABLE Input».",
      " Програма знайде кабель сама і заходитиме Zoom-клієнтом.", h("div", { style: "margin-top:6px" }, select)),
    h("small", { class: "hint" }, "Так вуха чують лише Zoom, а не YouTube чи музику. Як при цьому чути пару й самому — README, розділ «Вуха»."));
}

pages.assistant = () => {
  const linux = (meta.platform || "").startsWith("linux");
  const check = h("button", { class: "ghost" }, "Перевірити мозок і вуха");
  const checks = h("div", { class: "list" });
  check.onclick = () => busy(check, "Перевіряю…", async () => {
    const result = await api("POST", "/api/check");
    checks.replaceChildren(...result.checks.filter((item) => ["мозок", "вуха", "вуха в Zoom", "відеовирізки"].includes(item.area)).map((item) =>
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
      get("ears.enabled") && !meta.ears_installed && earsInstallBanner(),
      selectField("ears.model", "Модель розпізнавання", "Якщо комп'ютер не встигатиме за мовою, програма сама перейде на легшу модель.",
        [["auto", "автоматично за потужністю комп'ютера"], ["small", "small — швидка"], ["medium", "medium"], ["large-v3-turbo", "large-v3-turbo — точна"]]),
      linux && selectField("ears.source", "Пари в браузері: звідки брати звук", "Зі вкладки пари — вуха чують лише пару, навіть із вимкненим звуком. Весь звук браузера — запасний шлях: чує й інші вкладки браузера програми.",
        [["auto", "зі вкладки пари (рекомендовано)"], ["pulse", "весь звук браузера програми (PipeWire/PulseAudio)"]]),
      get("ears.enabled") && zoomEars(linux),
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

// --- Навчання -------------------------------------------------------------------

const STUDY_BADGE = {
  new: "", proposed: "accent", working: "warn", question: "accent", waiting: "warn", stuck: "bad", ready: "accent",
  approved: "warn", submitted: "ok", returning: "warn", returned: "warn", declined: "", skipped: "", failed: "bad", gone: "",
};
const STUDY_DONE = ["submitted", "declined", "skipped", "gone"];
const STUDY_YOURS = ["proposed", "question", "ready", "returned"];
const STUDY_BUSY = ["new", "working", "waiting", "approved", "returning"];
const STUDY_FILTERS = [
  ["active", "Активні", (task) => !STUDY_DONE.includes(task.status)],
  ["yours", "Чекають на тебе", (task) => STUDY_YOURS.includes(task.status)],
  ["busy", "У роботі", (task) => STUDY_BUSY.includes(task.status)],
  ["problems", "Не вийшло", (task) => ["stuck", "failed"].includes(task.status)],
  ["submitted", "Здані", (task) => task.status === "submitted"],
  ["skipped", "Пропущені", (task) => ["skipped", "declined", "gone"].includes(task.status)],
  ["all", "Усі", () => true],
];
const STUDY_TABS = [["overview", "Огляд"], ["tasks", "Завдання"], ["notes", "Конспекти"], ["settings", "Налаштування"]];

let studyTab = "overview";
try { studyTab = localStorage.getItem("autopara-study-tab") || "overview"; } catch (e) { /* не страшно */ }
let studyFilter = "active";
let studyCourse = "";
let studyQuery = "";
let studySelected = 0;
let studyDetails = null;
let studyEditing = 0;  // номер завдання, для якого відкрите поле зауважень
let studyLatest = null;
let agentInfo = null;
let agentChecking = false;
let studyTimer;
let notesData = null;
let noteSelected = "";
let noteText = "";
let notesQuery = "";
const studyBox = { head: null, tabs: null, body: null };

// --- Дрібниці -------------------------------------------------------------------

function studyDate(iso) {
  return new Date(iso).toLocaleDateString("uk-UA", { day: "numeric", month: "short" });
}

function dueInfo(iso) {
  if (!iso) return null;
  const due = new Date(iso);
  const hours = (due - Date.now()) / 36e5;
  const days = Math.round(hours / 24);
  if (hours < 0) return { text: `прострочено ${-days > 0 ? `на ${-days} дн.` : "сьогодні"}`, cls: "late" };
  if (hours < 24) return { text: due.getDate() === new Date().getDate() ? "сьогодні" : "завтра", cls: "late" };
  if (hours < 72) return { text: `через ${Math.max(1, days)} дн.`, cls: "soon" };
  return { text: `до ${studyDate(iso)}`, cls: "far" };
}

function dueChip(iso) {
  const info = dueInfo(iso);
  return info ? h("span", { class: `due ${info.cls}`, title: new Date(iso).toLocaleString("uk-UA") }, info.text) : null;
}

function elapsed(iso) {
  const minutes = Math.max(0, Math.round((Date.now() - new Date(iso)) / 6e4));
  return minutes < 60 ? `${minutes} хв` : `${Math.floor(minutes / 60)} год ${minutes % 60} хв`;
}

function fileSize(bytes) {
  if (bytes >= 2 ** 20) return `${(bytes / 2 ** 20).toFixed(1)} МБ`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} КБ`;
  return `${bytes} Б`;
}

function noteWhen(name) {
  const match = name.match(/^(\d{4})-(\d\d)-(\d\d)[ _](\d\d)-(\d\d)/);
  if (!match) return name;
  const date = new Date(+match[1], +match[2] - 1, +match[3], +match[4], +match[5]);
  return `${date.toLocaleDateString("uk-UA", { weekday: "short", day: "numeric", month: "short" })}, ${match[4]}:${match[5]}`;
}

function noteParts(name) {
  const match = name.match(/^(\d{4})-(\d\d)-(\d\d)[ _](\d\d)-(\d\d)/);
  if (!match) return { day: name, sub: "" };
  const date = new Date(+match[1], +match[2] - 1, +match[3]);
  return {
    day: date.toLocaleDateString("uk-UA", { day: "numeric", month: "short" }),
    sub: `${date.toLocaleDateString("uk-UA", { weekday: "short" })}, ${match[4]}:${match[5]}`,
  };
}

function ensureAgentInfo() {
  if (agentInfo || agentChecking) return;
  agentChecking = true;
  api("POST", "/api/study/agent")
    .then((info) => { agentInfo = info; agentChecking = false; studyDraw(true); })
    .catch(() => { agentChecking = false; });
}

function textareaField(path, label, hint, placeholder) {
  return field(label, hint, h("textarea", {
    rows: 3, placeholder: placeholder || "", value: get(path) ?? "", oninput: (event) => set(path, event.target.value),
  }));
}

// Markdown агента → DOM (без innerHTML: текст лишається текстом).
function mdInline(text) {
  const parts = [];
  const pattern = /(\*\*[^*]+\*\*|__[^_]+__|`[^`]+`|\[[^\]]+\]\(https?:\/\/[^)\s]+\)|\*[^*\s][^*]*\*|_[^_\s][^_]*_)/g;
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > last) parts.push(text.slice(last, match.index));
    const token = match[0];
    if (token.startsWith("**") || token.startsWith("__")) parts.push(h("b", {}, token.slice(2, -2)));
    else if (token.startsWith("`")) parts.push(h("code", {}, token.slice(1, -1)));
    else if (token.startsWith("[")) {
      const [, label, url] = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      parts.push(h("a", { href: url, target: "_blank", rel: "noopener" }, label));
    } else parts.push(h("i", {}, token.slice(1, -1)));
    last = match.index + token.length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

function md(text) {
  const root = h("div", { class: "md" });
  const lines = (text || "").replace(/\r/g, "").split("\n");
  let list = null;
  let paragraph = [];
  const flush = () => {
    if (paragraph.length) root.append(h("p", {}, mdInline(paragraph.join(" "))));
    paragraph = [];
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim().startsWith("```")) {
      flush(); list = null;
      const code = [];
      while (++i < lines.length && !lines[i].trim().startsWith("```")) code.push(lines[i]);
      root.append(h("pre", {}, h("code", {}, code.join("\n"))));
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      flush(); list = null;
      const cells = (row) => row.trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());
      const table = h("table", {}, h("thead", {}, h("tr", {}, cells(line).map((cell) => h("th", {}, mdInline(cell))))));
      const body = h("tbody");
      i += 1;
      while (i + 1 < lines.length && /^\s*\|.*\|\s*$/.test(lines[i + 1])) body.append(h("tr", {}, cells(lines[++i]).map((cell) => h("td", {}, mdInline(cell)))));
      table.append(body);
      root.append(table);
      continue;
    }
    let match;
    if (!line.trim()) { flush(); list = null; continue; }
    if ((match = line.match(/^(#{1,4})\s+(.*)$/))) { flush(); list = null; root.append(h(`h${match[1].length}`, {}, mdInline(match[2]))); continue; }
    if (/^\s*([-*_])\s*\1\s*\1[\s\1]*$/.test(line)) { flush(); list = null; root.append(h("hr")); continue; }
    if ((match = line.match(/^\s*>\s?(.*)$/))) { flush(); list = null; root.append(h("blockquote", {}, mdInline(match[1]))); continue; }
    if ((match = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/))) {
      flush();
      const ordered = /\d/.test(match[2]);
      if (!list || list.ordered !== ordered) {
        list = { ordered, element: h(ordered ? "ol" : "ul") };
        root.append(list.element);
      }
      list.element.append(h("li", {}, mdInline(match[3])));
      continue;
    }
    list = null;
    paragraph.push(line.trim());
  }
  flush();
  return root;
}

// --- Дії ----------------------------------------------------------------------------

async function studyAct(button, number, action, extra = {}) {
  const task = (studyLatest?.tasks || []).find((item) => item.number === number) || {};
  await busy(button, "…", async () => {
    const data = await api("POST", "/api/study/action", { number, action, version: task.version, ...extra });
    if (data.result) toast(data.result);
    studyEditing = 0;
    studyLatest = data;
    if (studySelected === number) studyDetails = null;
    studyDraw();
  });
}

function actionButton(task, label, cls, action, question) {
  const element = h("button", { class: cls }, label);
  element.onclick = () => { if (!question || confirm(question)) studyAct(element, task.number, action); };
  return element;
}

function taskActions(task, compact = false) {
  const actions = [];
  const add = (...items) => actions.push(...items);
  if (task.status === "proposed") add(actionButton(task, "▶️ Роби", "primary", "start"), actionButton(task, "❌ Не треба", "ghost", "no"));
  if (task.status === "returned") add(actionButton(task, "✏️ Переробити за коментарями", "primary", "rework"), actionButton(task, "❌ Не треба", "ghost", "no"));
  if ((task.status === "ready" || task.status === "failed") && task.files.length) {
    add(actionButton(task, "✅ Так, здати", "primary", "yes", `Здати «${task.title}» у Classroom?`));
  }
  if (task.status === "ready" || task.status === "stuck") {
    const redo = h("button", { class: "soft" }, task.status === "stuck" ? "💡 Підказати агентові" : "✏️ Переробити");
    redo.onclick = () => { studyEditing = task.number; if (compact) { studySelected = task.number; switchStudyTab("tasks"); } else studyDraw(); };
    add(redo);
  }
  if (["stuck", "failed", "declined", "gone"].includes(task.status)) add(actionButton(task, "🔁 Спробувати ще", "soft", "retry"));
  if (["ready", "stuck", "failed", "approved", "waiting"].includes(task.status)) add(actionButton(task, "❌ Не здавати", "ghost", "no"));
  if (task.status === "submitted" && task.submitted !== "раніше") add(actionButton(task, "↩️ Забрати з Classroom", "ghost", "back", `Скасувати здачу «${task.title}»?`));
  if (!compact) {
    if (task.folder) {
      const open = h("button", { class: "ghost" }, "📂 Файли");
      open.onclick = () => busy(open, "…", () => api("POST", "/api/study/open", { number: task.number }));
      add(open);
    }
    if (task.url) add(h("button", { class: "ghost", onclick: () => window.open(task.url, "_blank", "noopener") }, "↗ Classroom"));
  }
  return actions;
}

function askBox(task) {
  const text = h("textarea", { rows: 3, placeholder: "Твоя відповідь агентові…" });
  const send = h("button", { class: "primary" }, "Відповісти");
  send.onclick = () => {
    if (!text.value.trim()) { text.focus(); return; }
    studyAct(send, task.number, "answer", { comment: text.value.trim() });
  };
  return h("div", { class: "ask-box" },
    h("div", { class: "q" }, "❓ ", task.question),
    text,
    h("div", { class: "row", style: "margin-top:8px" }, send, actionButton(task, "🤷 Виріши сам", "soft", "decide"), actionButton(task, "❌ Не робити", "ghost", "no")));
}

function redoBox(task) {
  const text = h("textarea", {
    rows: 3,
    placeholder: task.status === "stuck" ? "Підказка: «Tinkercad недоступний — зроби в Wokwi», «варіант 7»…" : "Що змінити: «варіант 7, а не 5», «більше пояснень до коду»…",
  });
  const send = h("button", { class: "primary" }, "Переробити з цим");
  send.onclick = () => {
    if (!text.value.trim()) { text.focus(); return; }
    studyAct(send, task.number, "redo", { comment: text.value.trim() });
  };
  setTimeout(() => text.focus(), 0);
  return h("div", { class: "ask-box" }, h("div", { class: "q" }, task.status === "stuck" ? "💡 Підказка агентові" : "✏️ Що змінити"), text,
    h("div", { class: "row", style: "margin-top:8px" }, send, h("button", { class: "ghost", onclick: () => { studyEditing = 0; studyDraw(); } }, "Скасувати")));
}

function switchStudyTab(tab) {
  studyTab = tab;
  try { localStorage.setItem("autopara-study-tab", tab); } catch (e) { /* не страшно */ }
  studyDraw(true);
  window.scrollTo(0, 0);
}

function openTask(number) {
  studySelected = number;
  studyDetails = null;
  if (studyTab !== "tasks") switchStudyTab("tasks"); else studyDraw();
}

// --- Шапка: що відбувається зараз --------------------------------------------------

function studyHead() {
  const L = studyLatest;
  const open = (what, label) => {
    const element = h("button", { class: "ghost" }, label);
    element.onclick = () => busy(element, "…", () => api("POST", "/api/study/open", { what }));
    return element;
  };
  const head = h("div", { class: "study-head" },
    h("div", { style: "flex:1;min-width:260px" }, h("h1", {}, "Навчання"),
      h("p", { class: "lead" }, "Агент (Antigravity) робить завдання з Google Classroom і вижимки лекцій. Тобі приходять лише повністю готові роботи — і здаються вони тільки після твого «так».")));
  if (!L || L.off) return head;

  let pulse = "pulse";
  let what = "Агент вільний";
  let sub = "";
  if (L.activity) {
    pulse = "pulse busy";
    what = `Агент ${L.activity.what}${L.activity.title ? ` «${L.activity.title}»` : ""}`;
    sub = `${elapsed(L.activity.since)}${L.activity.attempt > 1 ? ` · спроба ${L.activity.attempt}` : ""}`;
  } else if (L.paused) {
    pulse = "pulse off"; what = "Агент на паузі"; sub = "нових робіт не бере; готові здавати можна";
  } else if (L.agent_problem) {
    pulse = "pulse warn"; what = "Агент недоступний"; sub = L.agent_problem;
  } else if (L.last_check) {
    const next = new Date(new Date(L.last_check).getTime() + (get("study.check_hours") || 3) * 36e5);
    sub = `Classroom перевірено ${when(L.last_check)} · наступна перевірка ~${next.toLocaleTimeString("uk-UA", { hour: "2-digit", minute: "2-digit" })}`;
  } else sub = "Classroom ще не перевіряли — перевірю між парами";

  const usage = L.agent_limit ? Math.min(100, Math.round((L.agent_used / L.agent_limit) * 100)) : 0;
  const check = h("button", { class: "ghost" }, "Перевірити Classroom");
  check.onclick = () => busy(check, "…", async () => toast((await api("POST", "/api/study/check")).result));
  const pause = h("button", { class: L.paused ? "primary" : "ghost" }, L.paused ? "▶️ Продовжити" : "⏸ Пауза");
  pause.onclick = () => busy(pause, "…", async () => {
    const result = await api("POST", "/api/study/pause", { paused: !L.paused });
    toast(result.result);
    studyLatest = result;
    studyDraw();
  });
  return h("div", {}, head,
    h("div", { class: "strip" },
      h("span", { class: pulse }),
      h("div", { style: "flex:1;min-width:220px" }, h("div", { class: "what" }, what), sub && h("div", { class: "muted small" }, sub)),
      h("div", { style: "min-width:170px" },
        h("div", { class: "muted small" }, `Запуски агента сьогодні: ${L.agent_used} з ${L.agent_limit}`),
        h("div", { class: `meter${usage > 80 ? " high" : ""}` }, h("div", { style: `width:${usage}%` }))),
      check, pause, open("works", "📂 Роботи")),
    L.last_error && h("div", { class: "banner bad" }, `Classroom: ${L.last_error}`));
}

function studyTabs() {
  const L = studyLatest;
  const yours = L && !L.off ? L.tasks.filter((task) => STUDY_YOURS.includes(task.status)).length : 0;
  const active = L && !L.off ? L.tasks.filter((task) => !STUDY_DONE.includes(task.status)).length : 0;
  const counts = { overview: yours, tasks: active };
  return h("div", { class: "study-tabs" }, STUDY_TABS.map(([key, label]) => h("button", {
    class: studyTab === key ? "on" : "", onclick: () => switchStudyTab(key),
  }, label, counts[key] > 0 && h("span", { class: `count${key === "tasks" ? " muted" : ""}` }, counts[key]),
    key === "settings" && L && L.agent_problem && h("span", { class: "count", style: "background:var(--warn)" }, "!"))));
}

// --- Огляд --------------------------------------------------------------------------

function studyOnboarding() {
  const L = studyLatest;
  if (get("study.enabled") && L && L.off) {
    const start = h("button", { class: "primary" }, "Увімкнути автовхід");
    start.onclick = () => busy(start, "…", async () => { await api("POST", "/api/start"); toast("Увімкнено"); });
    return h("div", { class: "hero" }, h("h2", {}, "Навчання ввімкнене, але програма зараз на паузі"),
      h("p", { class: "muted" }, `Навчання працює разом з автовходом на пари: ${L.off}.`), start);
  }
  ensureAgentInfo();
  const done = {
    agent: agentInfo ? agentInfo.ok : null,
    name: !!(get("study.full_name") || "").trim(),
    telegram: !!get("telegram.chat_id"),
  };
  const item = (ok, title, hint) => h("div", { class: "ci" },
    h("span", { class: `mark ${ok ? "yes" : "no"}` }, ok ? "✓" : ok === null ? "…" : "·"),
    h("div", {}, h("b", {}, title), h("div", { class: "muted small" }, hint)));
  const enable = h("button", { class: "primary" }, "Увімкнути навчання");
  enable.onclick = () => { set("study.enabled", true); save(); };
  const feature = (title, text) => h("div", { class: "feature" }, h("b", {}, title), h("span", {}, text));
  return h("div", { class: "hero" },
    h("h2", {}, "Агент, що робить завдання з Classroom"),
    h("p", { class: "muted", style: "margin:0;max-width:760px" }, "Між парами програма дивиться в Google Classroom, агент Antigravity виконує лаби й практичні повністю — з кодом, скріншотами, звітом і шпаргалкою до захисту. Ти переглядаєш і натискаєш «так» — програма здає."),
    h("div", { class: "features" },
      feature("✅ Лише після твого «так»", "Нічого не здається без тебе. Перша здача — пробна, зі скріном."),
      feature("🧪 Лише готові роботи", "Окремий прохід суворо перевіряє роботу проти умови й доробляє."),
      feature("❓ Питає, коли треба", "Незрозумілий варіант чи умова — питання в Telegram, відповідаєш — продовжує."),
      feature("📚 Вижимки лекцій", "Після пари — детальний конспект, а побажання викладача потрапляють у роботи."),
      feature("📒 Шпаргалка до захисту", "До кожної роботи — суть простими словами й імовірні питання."),
      feature("📬 Оцінки й повернення", "Пише про оцінку, а повернену роботу пропонує переробити за коментарями.")),
    h("b", {}, "Щоб почати"),
    h("div", { class: "checklist" },
      item(done.agent, "Antigravity CLI встановлено й виконано вхід", agentInfo ? (agentInfo.ok ? `Готово${agentInfo.version ? ` · ${agentInfo.version}` : ""}` : agentInfo.problem) : "Перевіряю…"),
      item(done.name, "ПІБ для титульної сторінки", done.name ? get("study.full_name") : "Вкладка «Налаштування» → «Про тебе»"),
      item(done.telegram, "Telegram-бот підключено", done.telegram ? "Готові роботи й питання приходитимуть туди" : "Можна й без нього — усе буде тут, у вікні"),
      item(true, "Вхід у Classroom", `Classroom відкривається в браузері програми під акаунтом ${get("study.account") || get("join.meet_account") || "з «Акаунтів»"}`)),
    h("div", { class: "row" }, enable, h("button", { class: "ghost", onclick: () => switchStudyTab("settings") }, "Спершу налаштувати")));
}

function compactCard(task) {
  return h("div", { class: "card", style: "margin-bottom:10px" },
    h("div", { class: "row" },
      h("b", { style: "cursor:pointer", onclick: () => openTask(task.number) }, task.title),
      h("span", { class: `badge ${STUDY_BADGE[task.status] || ""}` }, task.status_text), dueChip(task.due)),
    h("div", { class: "muted small" }, task.course, task.version > 1 ? ` · версія ${task.version}` : ""),
    task.status === "question" ? askBox(task) : null,
    task.status === "ready" && task.summary && h("div", { class: "small", style: "margin-top:6px;color:var(--muted)" }, task.summary.split("\n").find((line) => line.trim()) || ""),
    h("div", { class: "row", style: "margin-top:10px" }, taskActions(task, true),
      h("button", { class: "ghost", onclick: () => openTask(task.number) }, "Детальніше →")));
}

function studyLoading() {
  return h("div", { class: "card" }, h("div", { class: "empty" }, "Завантажую…"));
}

function studyOverview() {
  const L = studyLatest;
  if (!L) return studyLoading();
  if (L.off) return studyOnboarding();
  const tasks = L.tasks;
  const yours = tasks.filter((task) => STUDY_YOURS.includes(task.status));
  const busyTasks = tasks.filter((task) => STUDY_BUSY.includes(task.status) || task.status === "question");
  const submitted = tasks.filter((task) => task.status === "submitted");
  const upcoming = tasks.filter((task) => task.due && !STUDY_DONE.includes(task.status) && new Date(task.due) > Date.now() - 864e5 * 3)
    .sort((a, b) => new Date(a.due) - new Date(b.due));
  const next = upcoming.find((task) => new Date(task.due) > Date.now());
  const tile = (cls, num, label, sub, onclick) => h("button", { class: `tile ${cls}`, onclick }, h("span", { class: "num" }, num), h("span", { class: "label" }, label), sub && h("span", { class: "sub" }, sub));
  const filterTo = (key) => () => { studyFilter = key; switchStudyTab("tasks"); };
  const graded = submitted.filter((task) => task.grade);

  const notesCard = h("div", { class: "card" }, h("div", { class: "section-title" }, h("h2", {}, "Останні вижимки"), h("span", { class: "spacer" }),
    h("button", { class: "ghost", onclick: () => switchStudyTab("notes") }, "Усі конспекти →")));
  const notesList = h("div", { class: "empty" }, "Завантажую…");
  notesCard.append(notesList);
  loadNotes().then(() => {
    const recent = (notesData || []).flatMap((subject) => subject.lectures.map((lecture) => ({ ...lecture, subject: subject.name })))
      .sort((a, b) => b.date.localeCompare(a.date)).slice(0, 4);
    notesList.replaceWith(recent.length ? h("div", { class: "timeline" }, recent.map((lecture) => h("div", {
      class: "tl-row", onclick: () => { noteSelected = lecture.id; noteText = ""; switchStudyTab("notes"); },
    }, h("div", { class: "d" }, noteParts(lecture.date).day, h("small", {}, noteParts(lecture.date).sub)),
      h("div", {}, h("div", { class: "t" }, lecture.subject), h("div", { class: "c" }, lecture.preview || "вижимка ще готується")),
      lecture.ready ? h("span", { class: "badge ok" }, "готово") : h("span", { class: "badge" }, "лише розшифровка"))))
      : h("div", { class: "empty" }, "Вижимок ще немає — з'являться після першої пари, яку почують вуха."));
  });

  return [
    h("div", { class: "tiles" },
      tile(yours.length ? "accent" : "", yours.length, "Чекають на тебе", yours.length ? yours.map((task) => task.title).join(" · ") : "нічого не треба", filterTo("yours")),
      tile("warn", busyTasks.length, "Агент робить", L.activity ? `зараз: ${L.activity.title}` : "у черзі", filterTo("busy")),
      tile("ok", submitted.length, "Здано", graded.length ? `оцінено: ${graded.length}` : "з програми й раніше", filterTo("submitted")),
      tile("", next ? (dueInfo(next.due).text) : "—", "Найближчий термін", next ? next.title : "немає", next ? () => openTask(next.number) : filterTo("active"))),
    h("div", { class: "cols" },
      h("div", {},
        h("div", { class: "section-title" }, h("h2", {}, "Чекають на тебе"), yours.length > 0 && h("span", { class: "count" }, yours.length)),
        yours.length ? yours.map(compactCard) : h("div", { class: "card" }, h("div", { class: "empty" }, "Усе під контролем: ні питань, ні готових робіт без твого рішення."))),
      h("div", {},
        h("div", { class: "card" }, h("h2", {}, "Терміни"),
          upcoming.length ? h("div", { class: "timeline" }, upcoming.slice(0, 10).map((task) => h("div", { class: "tl-row", onclick: () => openTask(task.number) },
            h("div", { class: "d" }, studyDate(task.due), h("small", {}, new Date(task.due).toLocaleDateString("uk-UA", { weekday: "short" }))),
            h("div", {}, h("div", { class: "t" }, task.title), h("div", { class: "c" }, task.course)),
            h("span", { class: `badge ${STUDY_BADGE[task.status] || ""}` }, task.status_text))))
            : h("div", { class: "empty" }, "Найближчих термінів немає.")),
        notesCard)),
  ];
}

// --- Завдання -----------------------------------------------------------------------

function stepper(task) {
  const stage = { new: 0, proposed: 0, working: 1, question: 1, waiting: 1, stuck: 1, ready: 2, approved: 2, failed: 2, returning: 2, submitted: 3, returned: 3 }[task.status];
  if (stage === undefined) return null;
  const bad = ["stuck", "failed"].includes(task.status);
  const labels = ["Нове", task.status === "question" ? "Питання до тебе" : "Агент працює", "Готово — твоє «так»", task.status === "returned" ? "Повернуто" : "Здано"];
  return h("div", { class: "stepper" }, labels.map((label, index) => h("div", {
    class: `step${index < stage ? " done" : ""}${index === stage ? ` now${bad ? " bad" : ""}` : ""}${index === 3 && task.status === "submitted" ? " done" : ""}`,
  }, label)));
}

function fold(title, content, open = false) {
  return content ? h("details", { class: "fold", open }, h("summary", {}, title), h("div", {}, content)) : null;
}

function taskDetail(task) {
  if (!task) return h("div", { class: "card" }, h("div", { class: "empty" }, "Обери завдання зліва."));
  const details = studyDetails && studyDetails.number === task.number ? studyDetails : null;
  if (!details) {
    api("GET", `/api/study/task?number=${task.number}`).then((data) => {
      studyDetails = data;
      if (studySelected === task.number) drawTaskDetail();
    }).catch((e) => toast(e.message, true));
  }
  const info = details || task;
  const files = details ? details.files_info : [];
  const qa = [
    ...info.questions.flatMap((item) => [
      h("div", { class: "bubble agent" }, h("span", { class: "who" }, "Агент"), item.q),
      item.a && h("div", { class: "bubble me" }, h("span", { class: "who" }, "Ти"), item.a)]),
    ...info.feedback.map((item) => h("div", { class: "bubble me" }, h("span", { class: "who" }, "Твоє зауваження"), item)),
  ].filter(Boolean);
  return h("div", { class: "card" },
    h("div", { class: "detail-head" },
      h("h2", {}, info.title),
      h("span", { class: `badge ${STUDY_BADGE[info.status] || ""}` }, info.status_text),
      info.grade && h("span", { class: "badge ok" }, `📬 ${info.grade}`)),
    h("div", { class: "facts" }, info.course, dueChip(info.due),
      info.version > 0 && h("span", {}, `версія ${info.version}`),
      info.attempts > 0 && h("span", {}, `запусків агента: ${info.attempts}`),
      info.updated && h("span", {}, `оновлено ${when(info.updated)}`)),
    stepper(info),
    info.status === "question" ? askBox(info) : null,
    info.error && !["ready", "submitted"].includes(info.status) && h("div", { class: info.status === "stuck" || info.status === "failed" ? "banner bad" : "banner warn" }, info.error),
    h("div", { class: "row", style: "margin-bottom:6px" }, taskActions(info)),
    studyEditing === info.number && redoBox(info),
    !details && h("div", { class: "empty" }, "Завантажую подробиці…"),
    details && fold(`📎 Файли для здачі (${files.length})`, files.length ? h("div", { class: "files" }, files.map((file) => h("div", { class: "file-row" },
      h("span", {}, /\.pdf$/i.test(file.name) ? "📄" : /\.(mp4|mov|webm)$/i.test(file.name) ? "🎬" : /\.(zip|rar|7z)$/i.test(file.name) ? "🗜" : "📎"),
      h("span", { class: "name" }, file.name), h("span", { class: "size" }, fileSize(file.size))))) : null, info.status === "ready"),
    details && details.report_name && !files.length && h("div", { class: "muted small", style: "padding:8px 0" }, `Звіт буде названо: ${details.report_name}`),
    details && fold("📝 Що зроблено", details.result ? md(details.result) : null, info.status === "ready"),
    details && fold("📒 Шпаргалка до захисту", details.defense ? md(details.defense) : null),
    qa.length > 0 && fold(`💬 Питання й зауваження (${qa.length})`, h("div", { class: "qa" }, qa), info.status === "question"),
    details && fold("📋 Умова завдання", details.assignment ? h("div", { class: "plain" }, details.assignment) : null, !details.result),
    details && fold(`📚 Матеріали від викладача (${details.materials.length})`, details.materials.length ? h("div", { class: "files" }, details.materials.map((file) => h("div", { class: "file-row" },
      h("span", {}, "📎"), h("span", { class: "name" }, file.name.replace(/\.link\.txt$/, " (лише посилання)")), h("span", { class: "size" }, fileSize(file.size))))) : null));
}

let drawTaskDetail = () => {};

function studyTasksTab() {
  const L = studyLatest;
  if (!L) return studyLoading();
  if (L.off) return studyOnboarding();
  const courses = [...new Set(L.tasks.map((task) => task.course).filter(Boolean))].sort();
  const listBox = h("div", { class: "task-list" });
  const detailBox = h("div", { class: "detail" });
  const visible = () => {
    const test = STUDY_FILTERS.find(([key]) => key === studyFilter)[2];
    const query = studyQuery.trim().toLowerCase();
    return L.tasks.filter((task) => test(task) && (!studyCourse || task.course === studyCourse)
      && (!query || `${task.title} ${task.course}`.toLowerCase().includes(query)));
  };
  const drawList = () => {
    const tasks = visible();
    if (!studySelected && tasks.length) studySelected = tasks[0].number;
    listBox.replaceChildren(...(tasks.length ? tasks.map((task) => h("button", {
      class: `task-row${task.number === studySelected ? " on" : ""}`,
      onclick: () => { studySelected = task.number; studyDetails = null; studyEditing = 0; drawList(); drawTaskDetail(); },
    },
      h("span", { class: `bar ${STUDY_BADGE[task.status] || ""}` }),
      h("div", {}, h("div", { class: "t" }, task.title), h("div", { class: "c" }, `№${task.number} · ${task.course}`)),
      h("div", { class: "side" }, h("span", { class: `badge ${STUDY_BADGE[task.status] || ""}` }, task.status_text), dueChip(task.due))))
      : [h("div", { class: "card" }, h("div", { class: "empty" }, "Тут порожньо."))]));
  };
  drawTaskDetail = () => detailBox.replaceChildren(taskDetail(L.tasks.find((task) => task.number === studySelected)));
  drawList();
  drawTaskDetail();

  const search = h("input", { type: "text", placeholder: "Пошук за назвою…", value: studyQuery, oninput: (event) => { studyQuery = event.target.value; drawList(); } });
  return [
    h("div", { class: "filters" },
      STUDY_FILTERS.map(([key, label, test]) => {
        const count = L.tasks.filter(test).length;
        return h("button", { class: `filter${studyFilter === key ? " on" : ""}`, onclick: () => { studyFilter = key; studySelected = 0; studyDetails = null; studyDraw(); } },
          label, h("span", { class: "count muted" }, count));
      }),
      h("span", { class: "spacer" }),
      courses.length > 1 && h("select", { onchange: (event) => { studyCourse = event.target.value; studySelected = 0; studyDraw(); } },
        h("option", { value: "" }, "Усі предмети"), courses.map((course) => h("option", { value: course, selected: course === studyCourse }, course))),
      search),
    h("div", { class: "split" }, listBox, detailBox),
  ];
}

// --- Конспекти ------------------------------------------------------------------------

async function loadNotes(force = false) {
  if (notesData && !force) return;
  try { notesData = (await api("GET", "/api/study/notes")).subjects; } catch (e) { notesData = []; }
}

function studyNotesTab() {
  const listBox = h("div", {}, h("div", { class: "empty" }, "Завантажую…"));
  const noteBox = h("div", { class: "card" }, h("div", { class: "empty" }, "Обери пару зліва."));
  const showNote = async () => {
    if (!noteSelected) return;
    if (!noteText) {
      noteBox.replaceChildren(h("div", { class: "empty" }, "Завантажую…"));
      try { noteText = (await api("GET", `/api/study/note?id=${encodeURIComponent(noteSelected)}`)).text; } catch (e) { noteText = `_${e.message}_`; }
    }
    const [subject, date] = noteSelected.split("/");
    noteBox.replaceChildren(h("div", { class: "facts", style: "margin:0 0 12px" }, h("b", { style: "color:var(--text)" }, subject), h("span", {}, noteWhen(date))), md(noteText));
  };
  const drawList = () => {
    const query = notesQuery.trim().toLowerCase();
    const subjects = (notesData || []).map((subject) => ({
      ...subject,
      lectures: subject.lectures.filter((lecture) => !query || `${subject.name} ${lecture.preview}`.toLowerCase().includes(query)),
    })).filter((subject) => subject.lectures.length);
    if (!noteSelected && subjects.length) noteSelected = subjects[0].lectures[0].id;
    listBox.replaceChildren(...(subjects.length ? subjects.map((subject) => h("div", { class: "subject" },
      h("b", {}, subject.name, " ", h("span", { class: "count muted" }, subject.lectures.length)),
      subject.lectures.map((lecture) => h("button", {
        class: `lecture${lecture.id === noteSelected ? " on" : ""}`,
        onclick: () => { noteSelected = lecture.id; noteText = ""; drawList(); showNote(); },
      }, h("div", { class: "when" }, noteWhen(lecture.date), !lecture.ready && h("span", { class: "badge", style: "margin-left:6px" }, "розшифровка")),
        h("div", { class: "pv" }, lecture.preview || "вижимка ще готується")))))
      : [h("div", { class: "empty" }, notesData && notesData.length ? "Нічого не знайдено." : "Конспектів ще немає. Після кожної пари, яку почують вуха, агент зробить детальну вижимку.")]));
  };
  loadNotes(true).then(() => { drawList(); showNote(); });
  const openFolder = h("button", { class: "ghost" }, "📂 Теку конспектів");
  openFolder.onclick = () => busy(openFolder, "…", () => api("POST", "/api/study/open", { what: "notes" }));
  return [
    h("div", { class: "filters" },
      h("input", { type: "text", placeholder: "Пошук у конспектах…", value: notesQuery, oninput: (event) => { notesQuery = event.target.value; drawList(); } }),
      h("span", { class: "spacer" }), openFolder),
    h("div", { class: "notes" }, h("div", { class: "card" }, listBox), noteBox),
  ];
}

// --- Налаштування ----------------------------------------------------------------------

function agentSetup() {
  ensureAgentInfo();
  const result = h("div");
  const show = () => result.replaceChildren(agentInfo ? (agentInfo.ok
    ? h("div", { class: "banner ok" }, `Агент готовий${agentInfo.version ? ` · ${agentInfo.version}` : ""} · сьогодні запусків: ${agentInfo.used}`)
    : h("div", { class: "banner bad" }, agentInfo.problem)) : "");
  const check = h("button", { class: "primary" }, "Перевірити агента");
  check.onclick = () => busy(check, "Перевіряю…", async () => { agentInfo = await api("POST", "/api/study/agent"); show(); });
  show();
  const windows = (meta.platform || "").startsWith("win");
  return h("details", { class: "card", open: !agentInfo || !agentInfo.ok },
    h("summary", { style: "cursor:pointer" }, h("b", {}, "Підключення агента (Antigravity CLI)"),
      agentInfo && h("span", { class: `badge ${agentInfo.ok ? "ok" : "bad"}`, style: "margin-left:8px" }, agentInfo.ok ? "готовий" : "не готовий")),
    h("ol", { class: "steps", style: "margin-top:12px" },
      h("li", {}, h("div", {}, "Потрібна підписка Google з Antigravity. Встанови Antigravity CLI за інструкцією на ",
        h("a", { href: "https://antigravity.google", target: "_blank", rel: "noopener" }, "antigravity.google"), ".")),
      h("li", {}, h("div", {}, `Відкрий ${windows ? "PowerShell" : "термінал"}, введи `, h("code", {}, "agy"), " і увійди Google-акаунтом, на якому підписка. Це потрібно один раз.")),
      h("li", {}, h("div", {}, "Натисни «Перевірити агента». Пише, що команду не знайдено? Вкажи повний шлях у полі «Команда» нижче",
        windows ? " (напр. C:\\Users\\Ім'я\\AppData\\Local\\…\\agy.exe)." : ".")),
      h("li", {}, h("div", {}, "Classroom має бути відкритий у браузері програми під твоїм університетським акаунтом («Акаунти» → «Увійти в Google»)."))),
    h("div", { class: "row", style: "margin:6px 0 10px" }, check), result);
}

function listNumberControl() {
  const s = cfg.schedule;
  const holder = h("div");
  const pick = h("button", { class: "ghost", disabled: !s.group }, "Обрати себе зі списку групи");
  pick.onclick = () => busy(pick, "Завантажую…", async () => {
    const query = s.faculty_id && s.course && s.group_id
      ? `faculty_id=${s.faculty_id}&course=${s.course}&group_id=${s.group_id}`
      : `group=${encodeURIComponent(s.group)}`;
    const { students } = await api("GET", `/api/students?${query}`);
    holder.replaceChildren(h("select", { onchange: (event) => set("study.list_number", parseInt(event.target.value, 10) || undefined) },
      h("option", { value: "" }, "— визначати автоматично —"),
      students.map((name, index) => h("option", { value: index + 1, selected: get("study.list_number") === index + 1 }, `${index + 1}. ${name}`))));
  });
  const auto = studyLatest && studyLatest.list_number && !get("study.list_number") ? `Зараз визначено автоматично: №${studyLatest.list_number}. ` : "";
  return h("div", {},
    numberField("study.list_number", "Номер у списку групи", `${auto}Порожньо — програма знайде тебе в списку групи на сайті розкладу (за ПІБ).`, { optional: true }),
    (get("schedule.university") || "duikt") === "duikt" && h("div", { class: "row", style: "margin:-4px 0 14px" }, pick, holder));
}

function courseRules() {
  const wrap = h("div");
  const rules = () => get("study.courses") || [];
  const update = (index, key, value) => {
    const list = rules().map((rule) => ({ ...rule }));
    if (value) list[index][key] = value; else delete list[index][key];
    set("study.courses", list);
  };
  const known = () => [...new Set(((studyLatest && studyLatest.tasks) || []).map((task) => task.course))]
    .filter((course) => course && !rules().some((rule) => (rule.course || "").split("|").some((part) => part.trim() && course.toLowerCase().includes(part.trim().toLowerCase()))));
  const draw = () => {
    const add = (course = "") => { set("study.courses", [...rules(), { course }]); draw(); };
    const suggestions = known();
    wrap.replaceChildren(
      rules().length ? h("table", { class: "rules" },
        h("thead", {}, h("tr", {}, h("th", {}, "Предмет (частина назви курсу)"), h("th", {}, "Варіант"), h("th", {}, "Що робити"), h("th", {}, "Побажання до робіт"), h("th"))),
        h("tbody", {}, rules().map((rule, index) => h("tr", {},
          h("td", {}, h("input", { type: "text", value: rule.course || "", placeholder: "Бази даних|БД", oninput: (event) => update(index, "course", event.target.value) })),
          h("td", {}, h("input", { type: "text", value: rule.variant || "", placeholder: "авто", style: "max-width:90px", oninput: (event) => update(index, "variant", event.target.value) })),
          h("td", {}, h("select", { onchange: (event) => update(index, "mode", event.target.value === "auto" ? "" : event.target.value) },
            [["auto", "робити сам"], ["ask", "спершу спитати"], ["skip", "не чіпати"]].map(([value, text]) => h("option", { value, selected: (rule.mode || "auto") === value }, text)))),
          h("td", {}, h("input", { type: "text", value: rule.notes || "", placeholder: "Java 17, без Lombok; відео обов'язково", oninput: (event) => update(index, "notes", event.target.value) })),
          h("td", {}, h("button", { class: "icon", title: "Прибрати", onclick: () => { set("study.courses", rules().filter((_, i) => i !== index)); draw(); } }, "✕"))))))
        : h("div", { class: "empty" }, "Правил ще немає — агент робить усе за загальними налаштуваннями."),
      h("div", { class: "row", style: "margin-top:10px" },
        h("button", { class: "soft", onclick: () => add() }, "+ Додати предмет"),
        suggestions.length > 0 && h("select", { style: "width:auto", onchange: (event) => event.target.value && add(event.target.value) },
          h("option", { value: "" }, "+ з Classroom…"), suggestions.map((course) => h("option", { value: course }, course)))));
  };
  draw();
  return wrap;
}

function fileNamePreview() {
  const name = (get("study.full_name") || get("me.name") || "Прізвище Ім'я По батькові").trim().split(/\s+/);
  const short = `${name[0]}_${name.slice(1, 3).map((part) => `${part[0]}.`).join("")}`;
  const values = {
    short, surname: name[0], name: name.join(" "), group: get("study.group") || get("schedule.group") || "ТЦР-32",
    title: "ЛР №3 Колекції", course: "Java", kind: "Лб", n: "3", work: "Лб3", number: "12",
  };
  const template = get("study.file_name") || "{short}_{work}";
  const text = template.replace(/\{(\w+)\}/g, (match, key) => key in values ? values[key] : match).replace(/[\\/:*?"<>|\s]+/g, "_");
  return `${text}.${get("study.file_format") || "pdf"}`;
}

function studySettingsTab() {
  const university = { duikt: "Державний університет інформаційно-комунікаційних технологій", kpi: "КПІ ім. Ігоря Сікорського" }[get("schedule.university") || "duikt"];
  const preview = h("small", { class: "hint" }, `Приклад: ${fileNamePreview()}`);
  const fileName = textField("study.file_name", "Назва звіту",
    "{short} — Ляшко_Д.О., {surname}, {name}, {group}, {kind} — Лб/Пр/СР…, {n} — номер роботи, {work} — Лб3 (а без номера — назва), {title}, {course}.",
    { placeholder: "{short}_{work}" });
  fileName.addEventListener("input", () => { preview.textContent = `Приклад: ${fileNamePreview()}`; });
  const windows = (meta.platform || "").startsWith("win");
  const section = (id, title, ...content) => h("div", { class: "card", id: `study-${id}` }, h("h2", {}, title), ...content);
  const sections = [["main", "Основне"], ["me", "Про тебе"], ["variant", "Варіант"], ["courses", "Предмети"], ["look", "Оформлення"], ["talk", "Зв'язок"], ["agent", "Агент"]];
  return [
    agentSetup(),
    h("div", { class: "settings-nav" }, sections.map(([id, title]) => h("button", { class: "filter", onclick: () => document.getElementById(`study-${id}`)?.scrollIntoView({ behavior: "smooth", block: "start" }) }, title))),
    h("div", { class: "cols" },
      section("main", "Основне",
        checkField("study.enabled", "Виконувати завдання з Classroom", "Перевіряє «Завдання» між парами. Тести, захисти й контрольні не чіпає — лише нагадує про них."),
        h("div", { class: "grid" },
          textField("study.account", "Акаунт Classroom", "Порожньо — той самий, що для Meet.", { placeholder: get("join.meet_account") || "st…@stud.duikt.edu.ua" }),
          numberField("study.check_hours", "Перевіряти кожні, год", "", { min: 1 })),
        h("div", { style: "margin:4px 0 8px;font-weight:600" }, "Коли братися за нове завдання"),
        choiceField("study.start_mode", [
          { value: "auto", label: "Одразу", hint: "Агент починає сам, тобі приходить готова робота." },
          { value: "ask", label: "Спершу спитати «робити?»", hint: "Кнопки «▶️ Роби» / «❌ Не треба» на кожне нове завдання." },
        ]),
        chipsField("study.ask_first", "Завжди спершу питати, якщо в назві є", "Великі роботи: курсові, проєкти.", "слово і Enter"),
        h("div", { class: "grid" },
          numberField("study.overdue_days", "Прострочені — якщо не старші за, днів", "0 — прострочені не робити зовсім."),
          numberField("study.remind_hours", "Нагадувати за, год до терміну", "0 — не нагадувати.")),
        chipsField("study.skip_courses", "Курси, які не чіпати зовсім", "Частина назви курсу.", "назва курсу і Enter"),
        checkField("study.lecture_notes", "Вижимки лекцій", "Після пари агент робить детальний конспект з розшифровки. Конспекти бачить і агент, коли робить завдання з цього предмета."),
        checkField("study.watch_grades", "Стежити за оцінками", "Пише, коли здану роботу оцінили чи повернули, і пропонує переробити за коментарями викладача.")),
      section("me", "Про тебе — для титульної сторінки",
        h("div", { class: "grid" },
          textField("study.full_name", "ПІБ повністю", "", { placeholder: "Прізвище Ім'я По батькові" }),
          textField("study.group", "Група", "Порожньо — з розкладу.", { placeholder: get("schedule.group") || "" })),
        textField("study.university", "Університет", "Порожньо — за університетом розкладу.", { placeholder: university }),
        h("div", { class: "grid" },
          textField("study.faculty", "Факультет / інститут", "Порожньо — з матеріалів курсу."),
          textField("study.department", "Кафедра", "Порожньо — з матеріалів курсу.")),
        textField("study.specialty", "Спеціальність", "", { placeholder: "121 Інженерія програмного забезпечення" }),
        h("div", { class: "grid" },
          numberField("study.year", "Курс", "Порожньо — з розкладу.", { optional: true }),
          textField("study.city", "Місто", ""),
          textField("study.student_id", "Номер залікової", "Бувають варіанти за ним."))),
      section("variant", "Варіант",
        listNumberControl(),
        textField("study.variant_rule", "Як рахувати варіант, якщо в умові не сказано", "Порожньо — номер у списку групи. Або: «остача від ділення номера на 15», «остання цифра залікової».",
          { placeholder: "номер у списку групи" }),
        h("small", { class: "hint" }, "Якщо в умові своє правило (формула, літера прізвища…), агент порахує за ним і покаже розрахунок у звіті. Не вистачає даних — спитає тебе. Для окремого предмета варіант задається в «Предметах».")),
      section("talk", "Зв'язок з тобою",
        checkField("study.ask_questions", "Агент може питати мене через бота", "Незрозумілий варіант, суперечлива умова, потрібен твій акаунт чи пристрій. Вимкнено — вирішує сам і пише про це у звіті."),
        h("div", { class: "grid" },
          numberField("study.quiet_from", "Тихі години: з", "Повідомлення про навчання — без звуку.", { min: 0 }),
          numberField("study.quiet_to", "до", "Однакові — тихих годин немає.", { min: 0 })),
        h("small", { class: "hint" }, "Боту можна писати: «завдання», «питання», «перевір», «пауза», «продовжуй». Відповідь (reply) на повідомлення про роботу — відповідь агентові чи зауваження до роботи."))),
    section("courses", "Предмети",
      h("p", { class: "muted small", style: "margin-top:-4px" }, "Свій варіант, побажання викладача до робіт і що робити з предметом. Кілька варіантів назви — через «|»."),
      courseRules()),
    h("div", { class: "cols" },
      section("look", "Оформлення",
        h("div", { class: "grid" },
          selectField("study.file_format", "Формат звіту", "", [["pdf", "PDF"], ["docx", "Word (DOCX)"]]),
          textField("study.language", "Мова звітів", "", { placeholder: "українська" })),
        fileName, preview,
        textareaField("study.style", "Побажання до всіх робіт", "Агент дотримуватиметься їх у кожній роботі.", "Пиши просто, без води. Код з коментарями. Скріншоти з підписами."),
        textField("study.examples", "Тека зі зразками твоїх робіт", "Кілька твоїх звітів (PDF/DOCX) — агент оформлюватиме так само. Попередні роботи з того самого предмета він бачить і так.",
          { placeholder: windows ? "C:\\Users\\Ім'я\\Documents\\Звіти" : "~/Документи/Звіти" })),
      section("agent", "Агент",
        h("div", { class: "grid" },
          textField("agent.command", "Команда", "agy або повний шлях до нього.", { placeholder: "agy" }),
          numberField("agent.timeout_minutes", "Хвилин на запуск", "", { min: 5 })),
        h("div", { class: "grid" },
          numberField("agent.daily_limit", "Запусків на день", "Одна робота — 2–6 запусків.", { min: 1 }),
          numberField("agent.attempts", "Спроб довести до готового", "Кожна — виконання + перевірка.", { min: 1 })),
        chipsField("agent.args", "Параметри запуску", "Без --dangerously-skip-permissions агент не зможе сам запускати код. Модель тощо — тут, якщо твій agy це підтримує.", "параметр і Enter"),
        textField("study.folder", "Тека для робіт і конспектів", "Усередині: «Роботи/<предмет>/Лб 3 …» і «Конспекти/<предмет>/<дата>». Порожньо — у теці програми.",
          { placeholder: (studyLatest && studyLatest.folder) || "" }))),
  ];
}

// --- Складання сторінки ----------------------------------------------------------------

function studyDraw(full = false) {
  if (page !== "study" || !studyBox.body) return;
  studyBox.head.replaceChildren(studyHead());
  studyBox.tabs.replaceChildren(studyTabs());
  if (full || studyTab !== "settings") {
    const body = { overview: studyOverview, tasks: studyTasksTab, notes: studyNotesTab, settings: studySettingsTab }[studyTab] || studyOverview;
    studyBox.body.replaceChildren(...[body()].flat().filter(Boolean));
  }
}

function studyBusyTyping() {
  const focused = document.activeElement;
  return studyEditing || (focused && studyBox.body && studyBox.body.contains(focused) && ["INPUT", "TEXTAREA", "SELECT"].includes(focused.tagName));
}

pages.study = () => {
  studyBox.head = h("div");
  studyBox.tabs = h("div");
  studyBox.body = h("div");
  const pull = async () => {
    if (page !== "study") return;
    try {
      const data = await api("GET", "/api/study");
      const changed = JSON.stringify(data) !== JSON.stringify(studyLatest);
      studyLatest = data;
      if (changed && studyTab !== "notes") {
        studyBox.head.replaceChildren(studyHead());
        studyBox.tabs.replaceChildren(studyTabs());
        if (!studyBusyTyping() && studyTab !== "settings") {
          const selected = data.tasks?.find((task) => task.number === studySelected);
          if (studyDetails && selected && selected.updated !== studyDetails.updated) studyDetails = null;
          studyDraw();
        }
      } else if (studyLatest && studyLatest.activity) studyBox.head.replaceChildren(studyHead());  // лічильник часу
    } catch (e) { /* програма перезапускається */ }
    studyTimer = setTimeout(pull, 5000);
  };
  clearTimeout(studyTimer);
  studyEditing = 0;
  setTimeout(() => { studyDraw(true); pull(); }, 0);
  return [studyBox.head, studyBox.tabs, studyBox.body];
};

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
  main.classList.toggle("wide", page === "study");
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
