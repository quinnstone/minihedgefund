// Cloudflare Worker — Discord slash-command handler for MiniHedgeFund.
//
// Translates /logged, /closed, /undo, /positions, /book into reads/writes
// against data/actual_entries.txt and data/actual_book.json in the repo,
// committed via GitHub API. No LLM calls. No persistent state in the Worker
// itself — everything's in the repo.
//
// Discord interaction flow:
//   1. Discord POSTs the interaction here, signed with Ed25519
//   2. We verify the signature (mandatory per Discord)
//   3. Route by command name, perform GitHub API ops
//   4. Return a Discord-friendly response within 3s (the API deadline)

import { verifyKey, InteractionType, InteractionResponseType } from 'discord-interactions';

const GITHUB_API = 'https://api.github.com';

export default {
  async fetch(request, env) {
    if (request.method === 'GET') {
      return new Response('MiniHedgeFund Discord Worker — POST only', { status: 405 });
    }

    const signature = request.headers.get('x-signature-ed25519');
    const timestamp = request.headers.get('x-signature-timestamp');
    const rawBody = await request.text();

    if (!signature || !timestamp || !env.DISCORD_PUBLIC_KEY) {
      return new Response('missing signature headers', { status: 401 });
    }

    const valid = await verifyKey(rawBody, signature, timestamp, env.DISCORD_PUBLIC_KEY);
    if (!valid) {
      return new Response('bad signature', { status: 401 });
    }

    let interaction;
    try {
      interaction = JSON.parse(rawBody);
    } catch {
      return new Response('bad json', { status: 400 });
    }

    // PING — Discord sends this when validating the endpoint URL
    if (interaction.type === InteractionType.PING) {
      return jsonResponse({ type: InteractionResponseType.PONG });
    }

    if (interaction.type !== InteractionType.APPLICATION_COMMAND) {
      return jsonResponse({
        type: InteractionResponseType.CHANNEL_MESSAGE_WITH_SOURCE,
        data: { content: `unsupported interaction type ${interaction.type}` },
      });
    }

    const name = interaction.data?.name;
    const args = optionsToObj(interaction.data?.options);

    try {
      let content;
      switch (name) {
        case 'logged':    content = await handleLogged(args, env); break;
        case 'closed':    content = await handleClosed(args, env); break;
        case 'undo':      content = await handleUndo(env); break;
        case 'positions': content = await handlePositions(env); break;
        case 'book':      content = await handleBook(env); break;
        default:          content = `unknown command: /${name}`;
      }
      return jsonResponse({
        type: InteractionResponseType.CHANNEL_MESSAGE_WITH_SOURCE,
        data: { content: truncate(content, 1900) },
      });
    } catch (err) {
      return jsonResponse({
        type: InteractionResponseType.CHANNEL_MESSAGE_WITH_SOURCE,
        data: { content: `❌ Error: ${err.message}` },
      });
    }
  },
};

// ─── Command handlers ────────────────────────────────────────────────────

async function handleLogged(args, env) {
  return await appendEntry('buy', args, env);
}

async function handleClosed(args, env) {
  return await appendEntry('sell', args, env);
}

async function appendEntry(action, args, env) {
  const { ticker, shares, price } = args;
  if (!ticker || shares == null || price == null) {
    return '❌ Missing required arg (ticker, shares, price)';
  }
  const t = String(ticker).toUpperCase().trim();
  const date = args.date || todayUTC();
  const notes = (args.notes || '').trim();
  const noteSuffix = notes ? `  ${notes}` : '';
  const line = `${date}  ${t}  ${action}  ${shares}  ${price}${noteSuffix}`;

  const { content, sha } = await fetchFile(env.ENTRIES_PATH, env);
  const newContent = content.endsWith('\n') ? content + line + '\n' : content + '\n' + line + '\n';
  await updateFile(env.ENTRIES_PATH, newContent, sha,
    `actual: ${action} ${shares} ${t} @ $${price} [skip ci]`, env);

  const verb = action === 'buy' ? 'BUY' : 'SELL';
  return `✓ Logged ${verb} **${t}** ${shares} @ $${price}${notes ? ` _(${notes})_` : ''} on ${date}`;
}

async function handleUndo(env) {
  const { content, sha } = await fetchFile(env.ENTRIES_PATH, env);
  const lines = content.split('\n');
  let undoneIdx = -1;
  for (let i = lines.length - 1; i >= 0; i--) {
    const t = lines[i].trim();
    if (t && !t.startsWith('#')) {
      undoneIdx = i;
      break;
    }
  }
  if (undoneIdx < 0) return '⚠ Nothing to undo — no active entries found';

  const original = lines[undoneIdx];
  lines[undoneIdx] = `# ${original}   # undone ${todayUTC()}`;
  await updateFile(env.ENTRIES_PATH, lines.join('\n'), sha,
    `actual: undo entry [skip ci]`, env);
  return `✓ Undone (commented out): \`${original.trim()}\``;
}

async function handlePositions(env) {
  const book = await fetchBook(env);
  const positions = book.positions || [];
  if (positions.length === 0) {
    return `_No open positions in actual book._ AUM: $${(book.current_aum ?? 0).toFixed(2)}`;
  }
  const lines = [`**Open Positions** (${positions.length}):`];
  for (const p of positions) {
    const cur = p.current_price != null ? `$${p.current_price}` : '?';
    const pctStr = p.unrealized_pnl_pct != null
      ? `${(p.unrealized_pnl_pct * 100).toFixed(2)}%`
      : '?';
    const marker = p.unrealized_pnl > 0 ? '🟢' : p.unrealized_pnl < 0 ? '🔴' : '⚪';
    lines.push(`${marker} **${p.ticker}** — ${p.shares} sh @ $${p.avg_cost_per_share}, now ${cur} (${pctStr})`);
  }
  return lines.join('\n');
}

async function handleBook(env) {
  const book = await fetchBook(env);
  const aum = book.current_aum ?? 0;
  const cash = book.cash ?? 0;
  const cum = book.cumulative_return_pct ?? 0;
  const realized = book.realized_pnl ?? 0;
  const lines = [
    `**AUM:** $${aum.toFixed(2)}  ·  **Cash:** $${cash.toFixed(2)}`,
    `**Cumulative:** ${(cum * 100).toFixed(2)}%`,
  ];
  if (book.spy_return_from_inception_pct != null) {
    lines.push(`**SPY** since ${book.inception_date}: ${(book.spy_return_from_inception_pct * 100).toFixed(2)}%`);
  }
  if (book.alpha_pct != null) {
    const m = book.alpha_pct > 0 ? '🟢' : book.alpha_pct < 0 ? '🔴' : '⚪';
    lines.push(`**Alpha vs SPY:** ${m} ${(book.alpha_pct * 100).toFixed(2)}%`);
  }
  lines.push(`_Open positions: ${(book.positions || []).length}  ·  Realized P&L: $${realized.toFixed(2)}_`);
  if (book.leveraged) {
    lines.push(`⚠ **Cash negative** ($${cash.toFixed(2)}) — over-deployed vs $10k notional`);
  }
  return lines.join('\n');
}

// ─── GitHub API helpers ──────────────────────────────────────────────────

async function fetchFile(path, env) {
  const url = `${GITHUB_API}/repos/${env.GH_REPO}/contents/${encodeURIComponent(path)}`;
  const resp = await fetch(url, {
    headers: {
      'Authorization': `Bearer ${env.GH_PAT}`,
      'User-Agent': 'MiniHedgeFund-Worker',
      'Accept': 'application/vnd.github.v3+json',
    },
  });
  if (!resp.ok) {
    throw new Error(`GitHub read ${path} ${resp.status}`);
  }
  const data = await resp.json();
  const content = b64decode(data.content);
  return { content, sha: data.sha };
}

async function updateFile(path, content, sha, message, env) {
  const url = `${GITHUB_API}/repos/${env.GH_REPO}/contents/${encodeURIComponent(path)}`;
  const body = {
    message,
    content: b64encode(content),
    sha,
    committer: { name: 'minihedgefund-bot', email: 'bot@minihedgefund.invalid' },
  };
  const resp = await fetch(url, {
    method: 'PUT',
    headers: {
      'Authorization': `Bearer ${env.GH_PAT}`,
      'User-Agent': 'MiniHedgeFund-Worker',
      'Accept': 'application/vnd.github.v3+json',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    throw new Error(`GitHub write ${path} ${resp.status}: ${(await resp.text()).slice(0, 200)}`);
  }
}

async function fetchBook(env) {
  try {
    const { content } = await fetchFile(env.BOOK_PATH, env);
    return JSON.parse(content);
  } catch (err) {
    // book file may not exist yet, or just be unparseable — degrade quietly
    return {
      current_aum: 10000, cash: 10000, cumulative_return_pct: 0,
      spy_return_from_inception_pct: null, alpha_pct: null,
      positions: [], realized_pnl: 0, inception_date: null, leveraged: false,
    };
  }
}

// ─── Utilities ───────────────────────────────────────────────────────────

function optionsToObj(options) {
  const out = {};
  for (const o of (options || [])) out[o.name] = o.value;
  return out;
}

function todayUTC() {
  return new Date().toISOString().slice(0, 10);
}

function truncate(s, max) {
  return s.length <= max ? s : s.slice(0, max - 1) + '…';
}

function jsonResponse(obj) {
  return new Response(JSON.stringify(obj), {
    headers: { 'Content-Type': 'application/json' },
  });
}

// Workers' atob/btoa expect ASCII; we wrap them for UTF-8 safety.
function b64decode(b64) {
  const bin = atob(String(b64).replace(/\s+/g, ''));
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new TextDecoder().decode(bytes);
}

function b64encode(str) {
  const bytes = new TextEncoder().encode(str);
  let bin = '';
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}
