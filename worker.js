// Бот-помощник для кнопки «➕ Ещё 5 вакансий».
//
// Работает бесплатно на Cloudflare Workers (инструкция — в README, раздел «Кнопка „Ещё 5“»).
// Когда вы нажимаете кнопку или пишете боту /more, помощник запускает на GitHub
// задачу «Вакансии дня» в режиме «ещё». Через 2–3 минуты бот присылает 5 вакансий,
// которых ещё не было.
//
// Ещё он работает будильником: каждое утро запускает утреннюю подборку. Для этого в
// Cloudflare нужен Cron Trigger (Settings → Trigger Events) — расписание GitHub слишком
// часто опаздывает на несколько часов.
//
// Нужные секреты в настройках Worker (Settings → Variables and Secrets):
//   TELEGRAM_TOKEN   — токен бота (тот же, что на GitHub)
//   TELEGRAM_CHAT_ID — Id получателей через запятую: ваш, коллег или группы (как на GitHub)
//   GITHUB_TOKEN     — токен GitHub с правом запускать Actions

const GITHUB_REPO = "toxickkiwi/klac";
const GITHUB_BRANCH = "claude/minsk-job-parser-ncts5s";
const WORKFLOW_FILE = "daily.yml";
const EXTRA_COUNT = "5";
const BUTTON_TEXT = "➕ Ещё 5 вакансий";
const MOM_BUTTON_TEXT = "👩 Для мамы"; // личный поиск, только для владельца (первый Id)
const THREADS_BUTTON_TEXT = "🧵 Threads"; // посты из Threads, где ищут специалиста

// Публикация в канал с кнопками (команда /post). Бот должен быть админом канала.
const CHANNEL = "@klacprac";
const CHANNEL_NAME = "Клацай, працуй!";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/setup") {
      return setup(url, env);
    }
    if (url.pathname === "/telegram" && request.method === "POST") {
      if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== (await webhookSecret(env))) {
        return new Response("forbidden", { status: 403 });
      }
      const update = await request.json();
      try {
        await handleUpdate(update, env);
      } catch (err) {
        console.log("Ошибка:", err.stack || err);
      }
      return new Response("ok"); // Telegram ждёт ответ 200, иначе будет повторять
    }
    return new Response("Бот-помощник работает. Для настройки откройте /setup");
  },

  // Будильник: срабатывает по Cron Trigger из настроек Cloudflare.
  async scheduled(event, env, ctx) {
    const resp = await startWorkflow(env, { mode: "daily" });
    if (!resp.ok) {
      console.log("GitHub:", resp.status, await resp.text());
      const firstChat = String(env.TELEGRAM_CHAT_ID).split(",")[0].trim();
      await telegram(env, "sendMessage", {
        chat_id: firstChat,
        text: `⚠️ Не получилось запустить утреннюю подборку (GitHub ответил ${resp.status}). ` +
          "Проверьте GITHUB_TOKEN в настройках Cloudflare.",
      });
    }
  },
};

async function handleUpdate(update, env) {
  // Кому можно пользоваться ботом: Id людей или групп через запятую.
  const allowed = String(env.TELEGRAM_CHAT_ID).split(",").map((id) => id.trim());

  if (update.callback_query) {
    const query = update.callback_query;
    const chat = String(query.message ? query.message.chat.id : query.from.id);
    if (query.data === "publish" || query.data === "cancel") {
      return handlePublish(env, query, chat === allowed[0]);
    }
    const isMom = query.data === "mom" && chat === allowed[0];
    const isThreads = query.data === "threads";
    if (!allowed.includes(chat) || !(query.data === "more" || isMom || isThreads)) {
      return telegram(env, "answerCallbackQuery", { callback_query_id: query.id });
    }
    await telegram(env, "answerCallbackQuery", {
      callback_query_id: query.id,
      text: "Ищу ещё вакансии…",
    });
    if (isThreads) {
      return requestThreads(env, chat);
    }
    return isMom ? requestMom(env, chat) : requestMore(env, chat);
  }

  const message = update.message;
  const original = message && (message.text || message.caption);
  if (!original) {
    return;
  }
  const chat = String(message.chat.id);
  const text = original.trim().toLowerCase();
  if (text.startsWith("/id")) {
    // Помогает узнать Id группы или человека, чтобы добавить его в TELEGRAM_CHAT_ID.
    return telegram(env, "sendMessage", { chat_id: chat, text: `Id этого чата: ${chat}` });
  }
  if (!allowed.includes(chat)) {
    return telegram(env, "sendMessage", {
      chat_id: chat,
      text: `У вас пока нет доступа. Перешлите владельцу бота этот Id: ${chat}`,
    });
  }
  // /post в ответ на пост → предпросмотр с кнопками и «Опубликовать в канал».
  if (text.startsWith("/post")) {
    return chat === allowed[0]
      ? preparePost(env, chat, message, original)
      : telegram(env, "sendMessage", { chat_id: chat, text: "Публиковать в канал может только владелец бота." });
  }
  // Пересланный пост из канала (в нём есть строки с ⚡️) → текст для Threads.
  if (message.forward_origin || original.includes("⚡")) {
    return telegram(env, "sendMessage", {
      chat_id: chat,
      text: toThreads(original),
      reply_parameters: { message_id: message.message_id },
    });
  }
  if (text.startsWith("/start") || text.startsWith("/help")) {
    return telegram(env, "sendMessage", {
      chat_id: chat,
      text:
        "Привет! Каждое утро я присылаю подборку вакансий.\n\n" +
        `Нужно больше — нажмите «${BUTTON_TEXT}» внизу или напишите /more. ` +
        "Пришлю 5 вакансий, которых ещё не было.\n\n" +
        "Перешлите мне пост из канала — пришлю его в формате для Threads.",
      reply_markup: {
        keyboard: chat === allowed[0]
          ? [[{ text: BUTTON_TEXT }, { text: MOM_BUTTON_TEXT }], [{ text: THREADS_BUTTON_TEXT }]]
          : [[{ text: BUTTON_TEXT }], [{ text: THREADS_BUTTON_TEXT }]],
        resize_keyboard: true,
        is_persistent: true,
      },
    });
  }
  if (text.startsWith("/more") || text === BUTTON_TEXT.toLowerCase() || /^ещ[её]/.test(text)) {
    return requestMore(env, chat);
  }
  if (text.startsWith("/threads") || text === THREADS_BUTTON_TEXT.toLowerCase()) {
    return requestThreads(env, chat);
  }
  if (chat === allowed[0] && (text.startsWith("/mom") || text === MOM_BUTTON_TEXT.toLowerCase())) {
    return requestMom(env, chat);
  }
}

async function requestMore(env, chat) {
  const resp = await startWorkflow(env, { count: EXTRA_COUNT, mode: "extra" });
  const text = resp.ok
    ? "🔎 Ищу ещё 5 вакансий, пришлю через 2–3 минуты."
    : `⚠️ Не получилось запустить поиск (GitHub ответил ${resp.status}). ` +
      "Проверьте GITHUB_TOKEN в настройках Cloudflare.";
  if (!resp.ok) {
    console.log("GitHub:", resp.status, await resp.text());
  }
  return telegram(env, "sendMessage", { chat_id: chat, text });
}

async function requestThreads(env, chat) {
  const resp = await startWorkflow(env, { count: "5", mode: "extra", profile: "threads" });
  const text = resp.ok
    ? "🔎 Ищу посты в Threads, пришлю через 3–5 минут."
    : `⚠️ Не получилось запустить поиск (GitHub ответил ${resp.status}).`;
  if (!resp.ok) {
    console.log("GitHub:", resp.status, await resp.text());
  }
  return telegram(env, "sendMessage", { chat_id: chat, text });
}

async function requestMom(env, chat) {
  const resp = await startWorkflow(env, { count: "5", mode: "extra", profile: "mom" });
  const text = resp.ok
    ? "🔎 Ищу вакансии для мамы, пришлю через 2–3 минуты."
    : `⚠️ Не получилось запустить поиск (GitHub ответил ${resp.status}).`;
  if (!resp.ok) {
    console.log("GitHub:", resp.status, await resp.text());
  }
  return telegram(env, "sendMessage", { chat_id: chat, text });
}

function startWorkflow(env, inputs) {
  return fetch(
    `https://api.github.com/repos/${GITHUB_REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "klac-vacancies-bot",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: GITHUB_BRANCH, inputs }),
    },
  );
}

async function setup(url, env) {
  const missing = ["TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "GITHUB_TOKEN"].filter((k) => !env[k]);
  if (missing.length) {
    return new Response(`Не хватает секретов: ${missing.join(", ")}`, { status: 500 });
  }
  const hook = await telegram(env, "setWebhook", {
    url: `${url.origin}/telegram`,
    secret_token: await webhookSecret(env),
    allowed_updates: ["message", "callback_query"],
  });
  await telegram(env, "setMyCommands", {
    commands: [
      { command: "more", description: "Ещё 5 вакансий" },
      { command: "threads", description: "Посты из Threads, где ищут специалиста" },
      { command: "post", description: "Опубликовать пост в канал с кнопками" },
      { command: "id", description: "Узнать Id этого чата" },
    ],
  });
  const text = hook.ok
    ? "✅ Готово! Напишите боту /start — внизу появится кнопка «Ещё 5 вакансий»."
    : `⚠️ Telegram ответил ошибкой: ${hook.description}. Проверьте TELEGRAM_TOKEN.`;
  return new Response(text, { headers: { "Content-Type": "text/plain; charset=utf-8" } });
}

// ---------- публикация в канал с кнопками ----------

const POST_HELP =
  "Как опубликовать пост с кнопками:\n" +
  "1. Пришлите мне готовый пост (текст или фото с подписью).\n" +
  "2. Ответьте на него (свайп влево или «Ответить») командой с контактами, например:\n" +
  "/post @hr_anna https://rabota.by/vacancy/123\n\n" +
  "@username — кнопки «Откликнуться» (с готовым сообщением) и «Профиль», " +
  "ссылка — кнопка «Вакансия на сайте».\n" +
  "3. Я покажу, как будет выглядеть пост. Нажмите «✅ Опубликовать».";

// Готовое сообщение, которое подставится в чат с работодателем.
function applyText(vacancy) {
  return `Здравствуйте! Пишу по вакансии «${vacancy}» из канала ${CHANNEL_NAME} Хочу откликнуться.`;
}

async function preparePost(env, chat, message, original) {
  const post = message.reply_to_message;
  const args = original.trim().split(/\s+/).slice(1);
  if (!post || !args.length) {
    return telegram(env, "sendMessage", { chat_id: chat, text: POST_HELP });
  }
  const postText = post.text || post.caption || "";
  const vacancy = (postText.split("\n")[0] || "")
    .replace(/[\s\p{Extended_Pictographic}\uFE0F\u200d]+$/u, "")
    .trim();
  const rows = [];
  for (const arg of args) {
    const username = arg.match(/^@?([A-Za-z0-9_]{5,32})$/) && !arg.includes(".") ? arg.replace("@", "") : null;
    if (username) {
      const link = `https://t.me/${username}`;
      rows.push([
        { text: "💌 Откликнуться", url: `${link}?text=${encodeURIComponent(applyText(vacancy))}` },
        { text: "👤 Профиль", url: link },
      ]);
    } else if (/^https?:\/\//.test(arg)) {
      rows.push([{ text: "🔗 Вакансия на сайте", url: arg }]);
    }
  }
  if (!rows.length) {
    return telegram(env, "sendMessage", { chat_id: chat, text: POST_HELP });
  }
  // Предпросмотр — копия поста с кнопками; из неё же потом публикуем в канал.
  return telegram(env, "copyMessage", {
    chat_id: chat,
    from_chat_id: chat,
    message_id: post.message_id,
    reply_markup: {
      inline_keyboard: [
        ...rows,
        [{ text: "✅ Опубликовать", callback_data: "publish" }, { text: "✖️ Отмена", callback_data: "cancel" }],
      ],
    },
  });
}

async function handlePublish(env, query, isOwner) {
  const preview = query.message;
  if (!isOwner || !preview) {
    return telegram(env, "answerCallbackQuery", { callback_query_id: query.id });
  }
  if (query.data === "cancel") {
    await telegram(env, "answerCallbackQuery", { callback_query_id: query.id, text: "Отменено" });
    return telegram(env, "deleteMessage", { chat_id: preview.chat.id, message_id: preview.message_id });
  }
  const buttons = preview.reply_markup.inline_keyboard.slice(0, -1); // без «Опубликовать / Отмена»
  const result = await telegram(env, "copyMessage", {
    chat_id: CHANNEL,
    from_chat_id: preview.chat.id,
    message_id: preview.message_id,
    reply_markup: { inline_keyboard: buttons },
  });
  if (!result.ok) {
    await telegram(env, "answerCallbackQuery", { callback_query_id: query.id });
    return telegram(env, "sendMessage", {
      chat_id: preview.chat.id,
      text: `⚠️ Не получилось опубликовать: ${result.description}. ` +
        `Проверьте, что бот — администратор канала ${CHANNEL} с правом публикации.`,
    });
  }
  await telegram(env, "answerCallbackQuery", { callback_query_id: query.id, text: "Опубликовано ✅" });
  return telegram(env, "editMessageReplyMarkup", {
    chat_id: preview.chat.id,
    message_id: preview.message_id,
    reply_markup: { inline_keyboard: buttons },
  });
}

// ---------- пост из канала → пост для Threads ----------

const THREADS_FOOTER = "💌 Откликнуться — в нашем тг-канале Клацай, працуй! (ссылка в шапке)";
const THREADS_LIMIT = 500; // столько символов помещается в один пост Threads
const MAX_DUTIES = 6;

function toThreads(post) {
  const lines = post.split("\n").map((l) => l.trim()).filter(Boolean);
  // Первая строка — «Должность / Компания», эмодзи в конце убираем.
  const title = (lines[0] || "").replace(/[\s\p{Extended_Pictographic}\uFE0F\u200d]+$/u, "");

  // Строки с ⚡️: зарплата, формат работы (без адреса), опыт.
  const facts = lines
    .filter((l) => l.startsWith("⚡"))
    .map((l) => l.replace(/^⚡\uFE0F?\s*/, ""))
    .map((l) => l.split(" / ")[0].trim())
    .map((l) => `⚡️ ${l}`);

  const duties = shortDuties(sectionItems(lines, /обязанност|задачи|чем (предстоит|нужно)/i));
  const parts = [title, facts.join("\n")];
  if (duties.length) {
    parts.push(dutiesSentence(duties));
  }
  parts.push(THREADS_FOOTER);
  let result = parts.filter(Boolean).join("\n\n");
  // Не влезает в 500 символов — убираем обязанности по одной с конца.
  for (let n = duties.length - 1; result.length > THREADS_LIMIT && n >= 1; n--) {
    parts[2] = dutiesSentence(duties.slice(0, n));
    result = parts.filter(Boolean).join("\n\n");
  }
  return result;
}

// Пункты раздела после заголовка «⏺️ ОБЯЗАННОСТИ:» до следующего «⏺️».
function sectionItems(lines, heading) {
  const start = lines.findIndex((l) => l.startsWith("⏺") && heading.test(l));
  if (start < 0) {
    return [];
  }
  const items = [];
  for (const line of lines.slice(start + 1)) {
    if (line.startsWith("⏺") || line.startsWith("💌") || line.startsWith("#")) {
      break;
    }
    if (/^\.{3}|подробнее на сайте/i.test(line)) {
      continue;
    }
    items.push(line.replace(/^[—–\-•·*]\s*/, ""));
  }
  return items;
}

// «Вести проекты полного цикла: от брифа до сдачи…» → «вести проекты полного цикла».
function shortDuties(items) {
  return items
    .map((item) => item.split(/[:;,(]|\s[—–-]\s/)[0])
    .map((item) => item.replace(/[.!…\s]+$/, "").trim())
    .map((item) => trimDangling(cutAtPreposition(item.split(/\s+/)).slice(0, 4)).join(" "))
    .filter((item) => item.length > 2)
    .slice(0, MAX_DUTIES);
}

// «Коммуникация с клиентом на всех этапах» → «коммуникация с клиентом»: уточнения после
// предлога «на / в / по / для…» отбрасываем (если до него уже есть два слова).
const CUT_BEFORE = /^(на|в|во|по|для|от|до|при|за|из|без|через|между|согласно)$/i;
function cutAtPreposition(words) {
  const at = words.findIndex((w, i) => i >= 2 && CUT_BEFORE.test(w));
  return at > 0 ? words.slice(0, at) : words;
}

// Не оставляем в конце обрывки вроде «сроков и стоимости с».
const DANGLING = /^(и|а|или|с|со|в|во|на|по|для|от|до|к|ко|о|об|при|за|из|у|без|над|под|через|а также)$/i;
function trimDangling(words) {
  while (words.length > 1 && (DANGLING.test(words[words.length - 1]) || /,$/.test(words[words.length - 1]))) {
    words = words.slice(0, -1);
  }
  return words;
}

function dutiesSentence(duties) {
  const text = duties.map((d, i) => (i ? d.charAt(0).toLowerCase() + d.slice(1) : d)).join(", ");
  return text.charAt(0).toUpperCase() + text.slice(1) + "!";
}

// Секрет для проверки, что запрос пришёл именно от Telegram. Получается из токена бота,
// поэтому отдельно его хранить не нужно.
async function webhookSecret(env) {
  const data = new TextEncoder().encode("klac-webhook:" + env.TELEGRAM_TOKEN);
  const hash = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(hash)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 48);
}

async function telegram(env, method, payload) {
  const resp = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return resp.json();
}
