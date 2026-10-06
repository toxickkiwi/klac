// Бот-помощник для кнопки «➕ Ещё 5 вакансий».
//
// Работает бесплатно на Cloudflare Workers (инструкция — в README, раздел «Кнопка „Ещё 5“»).
// Когда вы нажимаете кнопку или пишете боту /more, помощник запускает на GitHub
// задачу «Вакансии дня» в режиме «ещё». Через 2–3 минуты бот присылает 5 вакансий,
// которых ещё не было.
//
// Нужные секреты в настройках Worker (Settings → Variables and Secrets):
//   TELEGRAM_TOKEN   — токен бота (тот же, что на GitHub)
//   TELEGRAM_CHAT_ID — ваш Id (тот же, что на GitHub)
//   GITHUB_TOKEN     — токен GitHub с правом запускать Actions

const GITHUB_REPO = "toxickkiwi/klac";
const GITHUB_BRANCH = "claude/minsk-job-parser-ncts5s";
const WORKFLOW_FILE = "daily.yml";
const EXTRA_COUNT = "5";
const BUTTON_TEXT = "➕ Ещё 5 вакансий";

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
};

async function handleUpdate(update, env) {
  const ownerChat = String(env.TELEGRAM_CHAT_ID);

  if (update.callback_query) {
    const query = update.callback_query;
    if (String(query.from.id) !== ownerChat || query.data !== "more") {
      return telegram(env, "answerCallbackQuery", { callback_query_id: query.id });
    }
    await telegram(env, "answerCallbackQuery", {
      callback_query_id: query.id,
      text: "Ищу ещё вакансии…",
    });
    return requestMore(env);
  }

  const message = update.message;
  if (!message || !message.text || String(message.chat.id) !== ownerChat) {
    return; // чужие сообщения игнорируем
  }
  const text = message.text.trim().toLowerCase();
  if (text.startsWith("/start") || text.startsWith("/help")) {
    return telegram(env, "sendMessage", {
      chat_id: ownerChat,
      text:
        "Привет! Каждое утро я присылаю подборку вакансий.\n\n" +
        `Нужно больше — нажмите «${BUTTON_TEXT}» внизу или напишите /more. ` +
        "Пришлю 5 вакансий, которых ещё не было.",
      reply_markup: {
        keyboard: [[{ text: BUTTON_TEXT }]],
        resize_keyboard: true,
        is_persistent: true,
      },
    });
  }
  if (text.startsWith("/more") || text === BUTTON_TEXT.toLowerCase() || /^ещ[её]/.test(text)) {
    return requestMore(env);
  }
}

async function requestMore(env) {
  const resp = await fetch(
    `https://api.github.com/repos/${GITHUB_REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "klac-vacancies-bot",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        ref: GITHUB_BRANCH,
        inputs: { count: EXTRA_COUNT, mode: "extra" },
      }),
    },
  );
  const text = resp.ok
    ? "🔎 Ищу ещё 5 вакансий, пришлю через 2–3 минуты."
    : `⚠️ Не получилось запустить поиск (GitHub ответил ${resp.status}). ` +
      "Проверьте GITHUB_TOKEN в настройках Cloudflare.";
  if (!resp.ok) {
    console.log("GitHub:", resp.status, await resp.text());
  }
  return telegram(env, "sendMessage", { chat_id: String(env.TELEGRAM_CHAT_ID), text });
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
    commands: [{ command: "more", description: "Ещё 5 вакансий" }],
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
