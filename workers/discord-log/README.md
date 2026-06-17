# MiniHedgeFund Discord Worker

Cloudflare Worker that turns Discord slash commands (`/logged`, `/closed`,
`/undo`, `/positions`, `/book`) into commits on this repo's
`data/actual_entries.txt`.

**One-time cost:** $0 (Cloudflare Workers free tier covers our volume forever).
**Setup time:** ~30 minutes of clicking.
**Maintenance:** essentially none — secrets don't expire unless you choose.

---

## Step 1 — Discord application (5 min)

1. Go to <https://discord.com/developers/applications>
2. Click **New Application**, name it "MiniHedgeFund"
3. On the **General Information** page, copy two values you'll need below:
   - **Application ID** (top of the page)
   - **Public Key** (under General Information)
4. On the **Bot** page → click **Reset Token** → copy the new token (you'll
   only see it once). This is the `BOT_TOKEN` you need below.
5. On the **OAuth2 → URL Generator** page, select scopes:
   `applications.commands` (slash commands), `bot`
6. For Bot Permissions, just `Send Messages` is fine
7. Copy the generated URL, open it in a new tab, invite the bot to your
   server (the one with your existing webhook)

---

## Step 2 — Cloudflare account (3 min)

1. Sign up at <https://cloudflare.com> (no credit card needed for the
   free Workers tier we're using)
2. That's it for now — Wrangler CLI handles the rest below

---

## Step 3 — GitHub fine-grained PAT (3 min)

A scoped, revocable token so the Worker can commit to your repo.

1. Go to <https://github.com/settings/tokens?type=beta>
2. **Generate new token**
3. **Token name:** `minihedgefund-discord-worker`
4. **Expiration:** 90 days (you'll re-roll quarterly — set a calendar reminder)
5. **Repository access:** Only select repositories → pick `quinnstone/minihedgefund`
6. **Permissions** → Repository permissions:
   - `Contents` → **Read and write**
   - leave everything else as "No access"
7. **Generate token**, copy the value (starts with `github_pat_…`) — you'll
   paste it into Wrangler in step 5, never share it

---

## Step 4 — Install Wrangler + dependencies (2 min)

From this `workers/discord-log/` directory:

```bash
cd workers/discord-log
npm install
npx wrangler login        # opens browser, sign in with Cloudflare account
```

---

## Step 5 — Set Worker secrets (2 min)

Two secrets need to be in the Worker's environment:

```bash
npx wrangler secret put GH_PAT
#   paste the github_pat_… token from step 3

npx wrangler secret put DISCORD_PUBLIC_KEY
#   paste the Public Key from step 1
```

These live in Cloudflare's secret store; you never see them again after
pasting. Not committed to the repo.

---

## Step 6 — Deploy the Worker (1 min)

```bash
npx wrangler deploy
```

Wrangler prints a URL like
`https://minihedgefund-discord-log.your-subdomain.workers.dev`. **Copy it.**

---

## Step 7 — Point Discord at the Worker (1 min)

1. Back at <https://discord.com/developers/applications> → your app →
   **General Information**
2. Find the field **Interactions Endpoint URL** and paste the Worker URL
   from step 6
3. Click **Save Changes**

Discord will immediately PING the Worker to verify it. If the page accepts
the URL without an error, signature verification is working. If it fails,
double-check you set `DISCORD_PUBLIC_KEY` in step 5 correctly.

---

## Step 8 — Register the slash commands (1 min)

Discord knows the Worker exists but doesn't know what commands to route.

```bash
cp .env.example .env
# edit .env, fill in DISCORD_APP_ID and DISCORD_BOT_TOKEN from step 1
npm run register
```

You should see:

```
✓ Registered 5 commands globally:
  /logged    — Log a paper-money BUY from your ThinkOrSwim account
  /closed    — Log a paper-money SELL from your ThinkOrSwim account
  /undo      — Comment out the most recent logged entry
  /positions — List your current open positions
  /book      — Show actual book aggregate
```

Global slash commands can take up to 1 hour to show up in Discord's
client. For faster testing, restart Discord (Ctrl/Cmd+R in the desktop
app) or use guild-scoped commands (see commands.json comments).

---

## Step 9 — Test it (10 sec)

In your Discord channel, type `/`. You should see the five commands in the
dropdown. Click `/logged`, fill in:

- ticker: `TEST`
- shares: `1`
- price: `100`
- notes: `verifying worker`

Bot replies: `✓ Logged BUY TEST 1 @ $100 (verifying worker) on 2026-MM-DD`

Check the repo — you should see a new commit
`actual: buy 1 TEST @ $100 [skip ci]` on `main`, with the line appended to
`data/actual_entries.txt`. The `parse-actual-entries.yml` workflow then
fires and updates `data/actual_book.json` within a minute.

Then `/undo` to remove the test entry.

---

## Pinnable cheat sheet for your Discord channel

After everything works, paste this into the channel and pin it:

```
📌 MiniHedgeFund — Discord Commands

/logged    Log a paper-money BUY from TOS
           Required: ticker, shares, price
           Optional: date (defaults today), notes
           Example: /logged ticker:NVDA shares:5.2 price:235.50

/closed    Log a paper-money SELL
           Same args as /logged

/undo      Comment out the most recent logged entry
           (preserves audit trail; doesn't hard-delete)

/positions Show your open positions with cost basis + current MV

/book      Show actual book aggregate: AUM, cum %, vs SPY, alpha

Tip: Discord autocompletes the args after you type / — no need to memorize syntax.
```

---

## Operations

- **View live logs:** `npm run tail` (streams from Cloudflare)
- **Re-deploy after code changes:** `npm run deploy`
- **Re-register commands after editing `scripts/commands.json`:** `npm run register`
- **Rotate the GitHub PAT** (do this every 90 days when it expires): repeat
  step 3, then `npx wrangler secret put GH_PAT` again

## Failure modes

| Symptom | Fix |
|---|---|
| Discord can't validate "Interactions Endpoint URL" | `DISCORD_PUBLIC_KEY` is wrong; redo step 5 |
| `/logged` returns "❌ Error: GitHub write 401" | `GH_PAT` expired or scoped wrong; redo step 3 + step 5 |
| Slash command doesn't appear in `/` dropdown | Wait up to 1hr for Discord global cmd propagation, or restart Discord client |
| Command fires but commit doesn't appear | `npm run tail` to see Worker logs; check GitHub PAT permissions |
| Worker URL changes | Edit Discord app's "Interactions Endpoint URL" with the new URL |

If anything goes weird, edit `data/actual_entries.txt` directly in the
GitHub web/mobile UI as a fallback — the `parse-actual-entries.yml`
workflow fires on any push to that file regardless of who made the commit.
