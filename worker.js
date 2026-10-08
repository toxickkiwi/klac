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
    const isMom = query.data === "mom" && chat === allowed[0];
    if (!allowed.includes(chat) || !(query.data === "more" || isMom)) {
      return telegram(env, "answerCallbackQuery", { callback_query_id: query.id });
    }
    await telegram(env, "answerCallbackQuery", {
      callback_query_id: query.id,
      text: "Ищу ещё вакансии…",
    });
    return isMom ? requestMom(env, chat) : requestMore(env, chat);
  }

  const message = update.message;
  if (!message || !message.text) {
    return;
  }
  const chat = String(message.chat.id);
  const text = message.text.trim().toLowerCase();
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
  if (text.startsWith("/start") || text.startsWith("/help")) {
    return telegram(env, "sendMessage", {
      chat_id: chat,
      text:
        "Привет! Каждое утро я присылаю подборку вакансий.\n\n" +
        `Нужно больше — нажмите «${BUTTON_TEXT}» внизу или напишите /more. ` +
        "Пришлю 5 вакансий, которых ещё не было.",
      reply_markup: {
        keyboard: chat === allowed[0]
          ? [[{ text: BUTTON_TEXT }, { text: MOM_BUTTON_TEXT }]]
          : [[{ text: BUTTON_TEXT }]],
        resize_keyboard: true,
        is_persistent: true,
      },
    });
  }
  if (text.startsWith("/more") || text === BUTTON_TEXT.toLowerCase() || /^ещ[её]/.test(text)) {
    return requestMore(env, chat);
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
      { command: "id", description: "Узнать Id этого чата" },
    ],
  });
  const text = hook.ok
    ? "✅ Готово! Напишите боту /start — внизу появится кнопка «Ещё 5 вакансий»."
    : `⚠️ Telegram ответил ошибкой: ${hook.description}. Проверьте TELEGRAM_TOKEN.`;
  return new Response(text, { headers: { "Content-Type": "text/plain; charset=utf-8" } });
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
