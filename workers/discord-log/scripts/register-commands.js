// One-time push of slash command definitions to Discord.
// Run with: `npm run register`. Re-run anytime you change commands.json.

import 'dotenv/config';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const APP_ID = process.env.DISCORD_APP_ID;
const BOT_TOKEN = process.env.DISCORD_BOT_TOKEN;

if (!APP_ID || !BOT_TOKEN) {
  console.error('ERROR: set DISCORD_APP_ID and DISCORD_BOT_TOKEN in .env');
  console.error('Get them from https://discord.com/developers/applications → your app');
  process.exit(1);
}

const __dirname = dirname(fileURLToPath(import.meta.url));
const commands = JSON.parse(await readFile(resolve(__dirname, 'commands.json'), 'utf8'));

const url = `https://discord.com/api/v10/applications/${APP_ID}/commands`;
const resp = await fetch(url, {
  method: 'PUT',
  headers: {
    'Authorization': `Bot ${BOT_TOKEN}`,
    'Content-Type': 'application/json',
  },
  body: JSON.stringify(commands),
});

if (!resp.ok) {
  console.error(`Discord API returned ${resp.status}:`);
  console.error(await resp.text());
  process.exit(1);
}

const data = await resp.json();
console.log(`✓ Registered ${data.length} commands globally:`);
data.forEach(c => console.log(`  /${c.name}  —  ${c.description}`));
console.log('\nNote: global commands take up to 1 hour to propagate to Discord clients.');
console.log('For faster testing, register to a specific guild via /applications/{APP_ID}/guilds/{GUILD_ID}/commands instead.');
