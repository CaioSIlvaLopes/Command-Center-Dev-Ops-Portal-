/**
 * Command Center (Dev & Ops Portal) - WhatsApp Relay Connector
 *
 * Pode ser executado de forma standalone (`npm start`) utilizando whatsapp-web.js
 * OU importado em um microsserviço Node.js já existente via:
 *   const { forwardToCommandCenter } = require('./index');
 */
require("dotenv").config();

const COMMAND_CENTER_URL =
  process.env.COMMAND_CENTER_URL || "http://127.0.0.1:8000/api/webhooks/whatsapp";
const COMMAND_CENTER_AUDIO_URL =
  process.env.COMMAND_CENTER_AUDIO_URL ||
  COMMAND_CENTER_URL.replace(/\/whatsapp\/?$/, "/whatsapp/audio");
const WEBHOOK_SECRET_TOKEN =
  process.env.WEBHOOK_SECRET_TOKEN || "dev-secret-key";

const TRIGGER_REGEX = /^(?:\/task|!cc|\/gemini)\b\s*/i;

/**
 * Extrai o identificador/nome do remetente diretamente do objeto `msg`
 * sem chamar `msg.getContact()` (que quebra em contas multi-device com
 * o erro `getAlternateUserWid - Invalid get call using deviceWid`).
 */
function extractSenderName(msg) {
  if (msg && msg._data && msg._data.notifyName) {
    return String(msg._data.notifyName).trim();
  }
  const rawJid = (msg && (msg.author || msg.from)) || "";
  if (rawJid) {
    return rawJid.split("@")[0].split(":")[0];
  }
  return "whatsapp";
}

/**
 * Encaminha uma mensagem de comando (`/task ...` ou `!cc ...`) para o Command Center Django.
 * Usa 127.0.0.1 por padrão para evitar falha de resolução IPv6 (`::1`) no Node 18+.
 *
 * @param {string} rawText - Texto integral da mensagem (ex: "/task Ajustar checkout #wsa !top3")
 * @param {string} sender - Identificador ou nome do remetente (ex: "@caio")
 * @param {string} [messageId] - ID opcional da mensagem no WhatsApp
 * @returns {Promise<object>} Resposta JSON do Command Center
 */
async function forwardToCommandCenter(rawText, sender = "", messageId = "") {
  let response;
  try {
    response = await fetch(COMMAND_CENTER_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Webhook-Token": WEBHOOK_SECRET_TOKEN,
      },
      body: JSON.stringify({
        text: rawText,
        sender: sender,
        message_id: messageId,
      }),
      signal: AbortSignal.timeout(8000),
    });
  } catch (err) {
    const causeMsg = err.cause ? ` (${err.cause.code || err.cause.message})` : "";
    throw new Error(
      `Falha ao conectar em ${COMMAND_CENTER_URL}${causeMsg}. Verifique se o Django está rodando (python manage.py runserver).`
    );
  }

  if (!response.ok) {
    const errBody = await response.text();
    throw new Error(`HTTP ${response.status}: ${errBody}`);
  }

  return await response.json();
}

/**
 * Encaminha um áudio explicitamente citado (quote + /task ou !cc) para o Command Center Django.
 * TRAVA 1 DE SEGURANÇA: Jamais encaminha áudios soltos sem quote explícito.
 */
async function forwardAudioToCommandCenter(
  audioBase64,
  mimeType = "audio/ogg",
  extraHint = "",
  sender = "",
  messageId = ""
) {
  let response;
  try {
    response = await fetch(COMMAND_CENTER_AUDIO_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Webhook-Token": WEBHOOK_SECRET_TOKEN,
      },
      body: JSON.stringify({
        audio_base64: audioBase64,
        mime_type: mimeType,
        extra_hint: extraHint,
        sender: sender,
        message_id: messageId,
        explicit_trigger: true,
      }),
      signal: AbortSignal.timeout(15000),
    });
  } catch (err) {
    const causeMsg = err.cause ? ` (${err.cause.code || err.cause.message})` : "";
    throw new Error(
      `Falha ao conectar em ${COMMAND_CENTER_AUDIO_URL}${causeMsg}. Verifique se o Django está rodando.`
    );
  }

  if (!response.ok) {
    const errBody = await response.text();
    throw new Error(`HTTP ${response.status}: ${errBody}`);
  }

  return await response.json();
}

/**
 * Inicializa o cliente standalone do whatsapp-web.js escutando `message_create`
 * (permite capturar mensagens enviadas pelo próprio usuário para si mesmo).
 */
function startStandaloneRelay() {
  const { Client, LocalAuth } = require("whatsapp-web.js");
  const qrcode = require("qrcode-terminal");

  const client = new Client({
    authStrategy: new LocalAuth({ dataPath: "./.wwebjs_auth" }),
    puppeteer: {
      headless: true,
      args: ["--no-sandbox", "--disable-setuid-sandbox"],
    },
  });

  client.on("qr", (qr) => {
    console.log("\n[WhatsApp Relay] Escaneie o QR Code abaixo no seu WhatsApp:");
    qrcode.generate(qr, { small: true });
  });

  client.on("ready", () => {
    console.log(
      `[WhatsApp Relay] Conectado e pronto! Escutando comandos (/task, !cc ou /gemini multi-linha + Quote Áudio IA) -> ${COMMAND_CENTER_URL}`
    );
  });

  // Cache leve em memória (últimos 15 áudios) apenas para contornar bug de @lid no getQuotedMessage()
  // NENHUM áudio é enviado ao Django sem que o usuário responda (quote) com /task, !cc ou /gemini (Trava 1).
  const recentAudioById = new Map();
  const lastAudioByChat = new Map();

  // `message_create` captura tanto mensagens recebidas quanto enviadas pelo próprio número
  client.on("message_create", async (msg) => {
    try {
      const msgType = (msg && msg.type) || (msg && msg._data && msg._data.type) || "";
      if (msgType === "ptt" || msgType === "audio") {
        const shortId = msg.id && msg.id.id ? msg.id.id : "";
        const chatKey = (msg.to || msg.from || "").split("@")[0];
        if (shortId) {
          recentAudioById.set(shortId, msg);
          if (recentAudioById.size > 15) {
            const oldestKey = recentAudioById.keys().next().value;
            recentAudioById.delete(oldestKey);
          }
        }
        if (chatKey) {
          lastAudioByChat.set(chatKey, msg);
        }
      }

      // TRAVA 1 DE SEGURANÇA: Áudios soltos (type === 'ptt' || 'audio') não têm body com /task ou !cc
      // e portanto são 100% ignorados aqui.
      const body = (msg.body || "").trim();
      if (!TRIGGER_REGEX.test(body)) {
        return;
      }

      const senderName = extractSenderName(msg);
      const messageId = msg.id && msg.id._serialized ? msg.id._serialized : "";

      // Verifica se o comando /task ou !cc é uma RESPOSTA (quote) a uma mensagem de áudio (ptt/audio)
      const rawQuoted = (msg && msg._data && msg._data.quotedMsg) || null;
      const quotedStanzaId = (msg && msg._data && msg._data.quotedStanzaID) || "";
      const hasQuote = Boolean(msg.hasQuotedMsg || rawQuoted || quotedStanzaId);

      if (hasQuote) {
        let quotedMsg = null;
        try {
          quotedMsg = await msg.getQuotedMessage();
        } catch (_) {}

        if (!quotedMsg && quotedStanzaId && recentAudioById.has(quotedStanzaId)) {
          quotedMsg = recentAudioById.get(quotedStanzaId);
        }

        const quotedType =
          (quotedMsg && quotedMsg.type) ||
          (rawQuoted && rawQuoted.type) ||
          (quotedStanzaId && recentAudioById.has(quotedStanzaId) ? "ptt" : "");

        if (quotedType === "ptt" || quotedType === "audio") {
          console.log(
            `[WhatsApp Relay] Gatilho de Áudio por Quote Explícito detectado de ${senderName}: ${body}`
          );
          let media = null;
          if (quotedMsg && typeof quotedMsg.downloadMedia === "function") {
            try {
              media = await quotedMsg.downloadMedia();
            } catch (_) {}
          }
          if (!media && quotedStanzaId && recentAudioById.has(quotedStanzaId)) {
            try {
              media = await recentAudioById.get(quotedStanzaId).downloadMedia();
            } catch (_) {}
          }
          if (!media) {
            const chatKey = (msg.to || msg.from || "").split("@")[0];
            const fallbackAudioMsg = lastAudioByChat.get(chatKey);
            if (fallbackAudioMsg && typeof fallbackAudioMsg.downloadMedia === "function") {
              try {
                media = await fallbackAudioMsg.downloadMedia();
              } catch (_) {}
            }
          }

          const b64Data = (media && media.data) || (rawQuoted && rawQuoted.body) || "";
          const mimeType =
            (media && media.mimetype) || (rawQuoted && rawQuoted.mimetype) || "audio/ogg";

          const audioResult = await forwardAudioToCommandCenter(
            b64Data,
            mimeType,
            body,
            senderName,
            messageId
          );
          if (audioResult.ok) {
            console.log(
              `[WhatsApp Relay] Áudio convertido em Tarefa #${audioResult.task_id} ("${audioResult.title}") [Cota Hoje: ${
                audioResult.quota ? audioResult.quota.used_today : "?"
              }/${audioResult.quota ? audioResult.quota.max_daily : 20}]`
            );
            try {
              await msg.react("🎧");
            } catch (_) {}
          } else {
            console.warn(
              `[WhatsApp Relay] Áudio bloqueado pela Trava de Segurança: ${audioResult.error || audioResult.status}`
            );
            try {
              await msg.react("⚠️");
            } catch (_) {}
          }
          return;
        }
      }

      console.log(`[WhatsApp Relay] Comando detectado de ${senderName}: ${body.split("\n")[0]}`);

      const result = await forwardToCommandCenter(body, senderName, messageId);
      const extraInfo = result.subtasks_created
        ? ` [+${result.subtasks_created} subtarefas, ${result.minutes_logged || 0}m]`
        : "";
      console.log(
        `[WhatsApp Relay] Tarefa #${result.task_id} ("${result.title}") criada em #${result.workspace} (Score: ${result.priority_score})${extraInfo}`
      );

      try {
        await msg.react("⚡");
      } catch (reactErr) {
        // Ignora caso a versão do WhatsApp Web restrinja reação na própria conversa
      }
    } catch (error) {
      console.error("[WhatsApp Relay] Erro ao encaminhar comando:", error.message);
      try {
        await msg.react("⚠️");
      } catch (_) {}
    }
  });

  client.initialize();
}

if (require.main === module) {
  startStandaloneRelay();
}

module.exports = {
  forwardToCommandCenter,
  forwardAudioToCommandCenter,
  startStandaloneRelay,
};
