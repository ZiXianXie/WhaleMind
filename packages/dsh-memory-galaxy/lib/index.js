/**
 * dsh-memory-galaxy —— 把记忆库画成一条旋臂星系。
 *
 * 只用 `ctx.inject(['webServer'], ...)` 局部注入：没有 web 服务时（headless 组合）
 * 插件照样加载，只是不挂路由，不会拖住插件树。
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { buildGalaxy } from './data.js';

export const name = 'dsh-memory-galaxy';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = path.join(HERE, '..', 'public');
const PREFIX = '/memory-galaxy';
const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
};

const DEFAULTS = {
  source: 'auto',        // auto | meow | qq-bridge | file
  title: '记忆银河',
  meowDb: '',            // 留空则用 <workspace>/.dsh-meow/memory.db
  qqBridgeDir: '',       // 留空则用 <workspace>/qq-bridge
  dataFile: '',          // source=file 时读这个 JSON
  cacheSeconds: 60,      // 数据缓存，避免每次刷新都重算
};

function sendJson(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'content-length': Buffer.byteLength(body),
  });
  res.end(body);
}

function uniq(list) {
  const seen = new Set();
  return list.filter((p) => p && !seen.has(p) && seen.add(p));
}

function resolveConfig(config = {}) {
  const cfg = { ...DEFAULTS, ...config };
  // 工作区可能是 process.cwd()，也可能在 DSH_WORKSPACE 里；插件也可能被别处加载，
  // 所以把「当前目录 / 插件目录上两级 / DSH_WORKSPACE」都作为候选，取第一个存在的。
  const workspace = process.env.DSH_WORKSPACE || process.cwd();
  const roots = uniq([
    workspace,
    process.cwd(),
    path.resolve(HERE, '..', '..', '..'),   // <workspace>/plugins/dsh-memory-galaxy/lib -> workspace
    path.resolve(HERE, '..', '..'),         // <workspace>/plugins -> workspace
  ]);

  const meowCandidates = cfg.meowDb
    ? [path.resolve(cfg.meowDb)]
    : uniq(roots.map((r) => path.join(r, '.dsh-meow', 'memory.db')));
  const qqBridgeCandidates = cfg.qqBridgeDir
    ? [path.resolve(cfg.qqBridgeDir)]
    : uniq(roots.map((r) => path.join(r, 'qq-bridge')));

  return {
    ...cfg,
    meowCandidates,
    qqBridgeCandidates,
    dataFile: cfg.dataFile ? path.resolve(cfg.dataFile) : '',
  };
}

/** 记忆数据：带时间缓存，避免频繁刷页面时反复读库 */
function createCache(cfg) {
  let cached = null;
  let cachedAt = 0;
  return function get(force = false) {
    const ttl = Math.max(0, Number(cfg.cacheSeconds) || 0) * 1000;
    const t = Date.now();
    if (!force && cached && t - cachedAt < ttl) return cached;
    cached = buildGalaxy(cfg);
    cachedAt = t;
    // 数据本身带来源标记，页面上要显示
    cached.meta.title = cfg.title;
    return cached;
  };
}

function serveStatic(req, res, urlPath) {
  const rel = urlPath === `${PREFIX}/` || urlPath === PREFIX ? 'index.html' : urlPath.slice(PREFIX.length + 1);
  const file = path.join(PUBLIC_DIR, decodeURIComponent(rel));
  if (!file.startsWith(PUBLIC_DIR) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    res.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' });
    res.end('not found');
    return;
  }
  res.writeHead(200, {
    'content-type': MIME[path.extname(file).toLowerCase()] || 'application/octet-stream',
    'cache-control': 'no-store',
  });
  fs.createReadStream(file).pipe(res);
}

export function apply(ctx, config = {}) {
  const cfg = resolveConfig(config);
  const getGalaxy = createCache(cfg);

  ctx.inject(['webServer'], (webCtx) => {
    const handler = async (req, res) => {
      try {
        const url = new URL(req.url ?? '/', 'http://localhost');
        const p = url.pathname;

        if (req.method === 'GET' && (p === `${PREFIX}/api/data` || p === `${PREFIX}/api/data/`)) {
          const force = url.searchParams.get('refresh') === '1';
          return sendJson(res, 200, getGalaxy(force));
        }
        if (req.method === 'GET' && (p === `${PREFIX}/api/info` || p === `${PREFIX}/api/info/`)) {
          const g = getGalaxy();
          const firstExisting = (list) => list.find((p) => fs.existsSync(p)) || list[0];
          return sendJson(res, 200, {
            ok: true,
            title: cfg.title,
            source: cfg.source,
            paths: {
              workspace: process.env.DSH_WORKSPACE || process.cwd(),
              meow: firstExisting(cfg.meowCandidates),
              qqBridge: firstExisting(cfg.qqBridgeCandidates),
              dataFile: cfg.dataFile || null,
            },
            meta: g.meta,
          });
        }
        if (req.method === 'GET' && (p === PREFIX || p.startsWith(`${PREFIX}/`))) {
          return serveStatic(req, res, p);
        }
        res.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' });
        res.end('not found');
      } catch (error) {
        if (typeof ctx.logger?.warn === 'function') {
          ctx.logger.warn(`[memory-galaxy] ${error?.message ?? String(error)}`);
        }
        res.writeHead(500, { 'content-type': 'application/json; charset=utf-8' });
        res.end(JSON.stringify({ ok: false, error: String(error?.message ?? error) }));
      }
    };

    try {
      const dispose = webCtx.webServer.register({ kind: 'prefix', path: PREFIX, handler });
      if (typeof ctx.logger?.info === 'function') {
        ctx.logger.info(`[memory-galaxy] mounted at ${PREFIX}`);
      }
      if (typeof ctx.on === 'function' && typeof dispose === 'function') {
        ctx.on('dispose', () => { try { dispose(); } catch { /* ignore */ } });
      }
    } catch (error) {
      if (typeof ctx.logger?.warn === 'function') {
        ctx.logger.warn(`[memory-galaxy] mount failed: ${error?.message ?? String(error)}`);
      }
    }
  });
}

export default { name, apply };
