// Native bridge command adapter. All food behavior belongs to this skill.
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const python = fileURLToPath(new URL('../.private/.venv/Scripts/python.exe', import.meta.url));
const script = fileURLToPath(new URL('./meal_forms.py', import.meta.url));
const inFlight = new Set();

export function runPython(event) {
  return new Promise((resolve, reject) => {
    const child = spawn(python, [script], {
      windowsHide: true, shell: false,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let output = '';
    const timer = setTimeout(() => { child.kill(); reject(new Error('timeout')); }, 19 * 60 * 1000);
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', chunk => { output += chunk; if (output.length > 100000) child.kill(); });
    child.stderr.on('data', () => {}); // Do not expose CLI/credential diagnostics to chat.
    child.on('error', err => { clearTimeout(timer); reject(err); });
    child.on('close', () => {
      clearTimeout(timer);
      try { resolve(JSON.parse(output.trim())); } catch { reject(new Error('invalid response')); }
    });
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify(event));
  });
}

export async function handleBodyCoach(args, ctx, invoke = runPython) {
  const match = /^meal ([a-f0-9]{32})$/.exec(args);
  if (!ctx.fromCardAction || !match) return;
  // Configured chat and owner, date, option membership and dedup are checked in Python.
  const nonce = match[1];
  if (inFlight.has(nonce)) return;
  inFlight.add(nonce);
  const event = { nonce, chat_id: ctx.msg.chatId, sender_id: ctx.msg.senderId, form_value: ctx.formValue };
  const task = (async () => {
    let text;
    try {
      const result = await invoke(event);
      text = result.text;
    } catch {
      text = '三餐记录暂未完成，请稍后重试；请勿把无反馈当成记录成功。';
    }
    await ctx.channel.send(ctx.msg.chatId, { text }, { replyTo: ctx.msg.messageId });
  })();
  // Return immediately so Feishu's callback acknowledgement does not time out.
  void task.catch(() => {}).finally(() => inFlight.delete(nonce));
}
