// Observe the native adapter's serialized fetch body; never record headers.
import { appendFileSync, readFileSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';

export const name = 'roller-request-journal';
export const inject = ['subprocess', 'attachments'];
export function apply(ctx) {
  const original = globalThis.fetch;
  let turn, count = 0;
  const journal = process.env.ROLLER_DEEPSEEK_JOURNAL;
  const attachments = ctx.attachments;
  const saveImage = attachments.saveImage;
  const saveImages = attachments.saveImages;
  const pending = new Map();
  const recordImage = (ref) => {
    if (!ref?.attachmentId) throw new Error('DeepSeek image has no attachment ID');
    // Concurrent saveImage/saveImages callers await the same durable write.
    // IDs belong to the attachment service, not to the active user session.
    if (!pending.has(ref.attachmentId)) {
      const write = (async () => {
        const stored = await attachments.readImage(ref);
        const data = Buffer.from(stored.data);
        if (!data.length || data.length > 50 * 1024 * 1024)
          throw new Error('DeepSeek image exceeds the journal byte limit or is empty');
        appendFileSync(journal, JSON.stringify({
          type: 'attachment', attachment: stored.ref, mimeType: stored.ref.mediaType,
          data: data.toString('base64'), sha256: createHash('sha256').update(data).digest('hex'),
        }) + '\n');
      })();
      pending.set(ref.attachmentId, write);
      write.catch(() => pending.delete(ref.attachmentId));
    }
    return pending.get(ref.attachmentId);
  };
  attachments.saveImage = async function (input) {
    const ref = await saveImage.call(this, input);
    await recordImage(ref);
    return ref;
  };
  attachments.saveImages = async function (inputs) {
    const refs = await saveImages.call(this, inputs);
    for (const ref of refs) await recordImage(ref);
    return refs;
  };
  // dsh scrubs credential-shaped ambient names. Restore only environment
  // entries the Roller task or --pass-env explicitly requested, using the
  // native subprocess service's deliberate env override seam.
  const service = ctx.subprocess;
  const spawnTerminal = service.spawnTerminal;
  service.spawnTerminal = function (spec) {
    const config = JSON.parse(readFileSync('/roller/deepseek-state/turn.json', 'utf8'));
    const env = Object.fromEntries(config.tool_env_names.filter(n => process.env[n] !== undefined)
      .map(n => [n, process.env[n]]));
    return spawnTerminal.call(service, {...spec, env: {...env, ...spec.env}});
  };
  globalThis.fetch = async function (input, init) {
    const url = typeof input === 'string' ? input : input.url ?? String(input);
    if (new URL(url).pathname.endsWith('/chat/completions')) {
      const config = JSON.parse(readFileSync('/roller/deepseek-state/turn.json', 'utf8'));
      if (turn !== config.turn) { turn = config.turn; count = 0; }
      if (++count > config.max_steps) {
        const error = `Roller DeepSeek max steps exceeded (${config.max_steps})`;
        appendFileSync(journal, JSON.stringify({type: 'runner_error', turn, error}) + '\n');
        writeFileSync('/roller/deepseek-state/limit.json', JSON.stringify({turn, error}));
        throw new Error(error);
      }
      if (typeof init?.body !== 'string') throw new Error('DeepSeek request body is not serialized JSON');
      const body = JSON.parse(init.body);
      appendFileSync(journal, JSON.stringify({type: 'request', turn, body}) + '\n');
    }
    return original.call(globalThis, input, init);
  };
  ctx.on('dispose', () => {
    globalThis.fetch = original;
    service.spawnTerminal = spawnTerminal;
    attachments.saveImage = saveImage;
    attachments.saveImages = saveImages;
  });
}
