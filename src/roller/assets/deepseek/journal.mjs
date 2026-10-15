// Observe the native adapter's serialized fetch body; never record headers.
import { appendFileSync, readFileSync, writeFileSync } from 'node:fs';

export const name = 'roller-request-journal';
export const inject = ['subprocess'];
export function apply(ctx) {
  const original = globalThis.fetch;
  let turn, count = 0;
  const journal = process.env.ROLLER_DEEPSEEK_JOURNAL;
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
      if (config.capture_calls) {
        const headers = new Headers(init.headers ?? (typeof input === 'object' ? input.headers : undefined));
        headers.set('x-roller-native-session-id', config.session_id);
        init = {...init, headers};
      }
    }
    return original.call(globalThis, input, init);
  };
  ctx.on('dispose', () => { globalThis.fetch = original; service.spawnTerminal = spawnTerminal; });
}
