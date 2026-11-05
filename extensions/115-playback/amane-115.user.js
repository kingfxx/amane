// ==UserScript==
// @name         Amane 115 播放
// @namespace    amane-local-extensions
// @version      0.1.0
// @description  按番号搜索已登录的 115 文件, 点击后打开 115master
// @match        http://localhost:8000/*
// @match        http://127.0.0.1:8000/*
// @grant        GM_xmlhttpRequest
// @grant        GM_openInTab
// @connect      webapi.115.com
// @run-at       document-idle
// ==/UserScript==

(() => {
  "use strict";

  function numberPattern(number) {
    const fc2 = /^FC2[-_\s]*(?:PPV[-_\s]*)?(\d+)$/i.exec(number.trim());
    if (fc2)
      return new RegExp(
        `(?:^|[^A-Z0-9])(?:FC2[-_\\s]*(?:PPV[-_\\s]*)?)?${fc2[1]}(?![A-Z0-9])`,
        "i",
      );
    const parts = number
      .trim()
      .toUpperCase()
      .split(/[-_\s]+/)
      .filter(Boolean);
    if (!parts.length) throw new Error("影片番号为空");
    const escaped = parts.map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    return new RegExp(`(?:^|[^A-Z0-9])${escaped.join("[-_\\s]*")}(?![A-Z0-9])`, "i");
  }

  function searchTerms(number) {
    const fc2 = /^FC2[-_\s]*(?:PPV[-_\s]*)?(\d+)$/i.exec(number.trim());
    return fc2
      ? [fc2[1]]
      : [...new Set([number, number.replace(/[-_\s]/g, ""), number.replace(/[-_]/g, " ")])];
  }

  function candidates(rows, number) {
    const pattern = numberPattern(number);
    const seen = new Set();
    return rows.flatMap((row) => {
      const name = String(row.n ?? row.name ?? row.file_name ?? "");
      const pickcode = String(row.pc ?? row.pick_code ?? row.pickcode ?? "");
      if (
        !/^[a-zA-Z0-9]+$/.test(pickcode) ||
        seen.has(pickcode) ||
        !pattern.test(name) ||
        !/\.(mp4|mkv|avi|mov|m4v|ts|wmv|flv|rmvb|webm)$/i.test(name) ||
        /(?:trailer|preview|sample|预告|预览)/i.test(name)
      )
        return [];
      seen.add(pickcode);
      const size = Number(row.s ?? row.size ?? 0);
      return [{ name, pickcode, size: Number.isFinite(size) && size > 0 ? size : 0 }];
    });
  }

  function masterUrl(pickcode) {
    if (!/^[a-zA-Z0-9]+$/.test(pickcode)) throw new Error("无效的播放标识");
    return `https://115.com/web/lixian/master/video/?pick_code=${encodeURIComponent(pickcode)}`;
  }

  function parsePage(body) {
    if (!body || typeof body !== "object") throw new Error("115 返回格式异常");
    if (body.state === false || body.success === false) {
      const code = Number(body.errno ?? body.errNo ?? body.code);
      throw new Error(
        [99, 911, 401].includes(code)
          ? "115 需要登录或完成验证, 请打开 115 处理后重新查询"
          : "115 搜索失败或受到限制, 请稍后重新查询",
      );
    }
    const rows = Array.isArray(body.data) ? body.data : body.data?.list;
    if (!Array.isArray(rows) || rows.some((row) => !row || typeof row !== "object")) {
      throw new Error("115 返回格式异常, 请确认登录状态");
    }
    const count = Number(body.count ?? body.data?.count);
    return { rows, count: Number.isFinite(count) && count >= 0 ? count : null };
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { numberPattern, searchTerms, candidates, masterUrl, parsePage };
    return;
  }

  const cache = new Map();
  const pending = new Map();
  let currentPath = "";
  let generation = 0;
  let controller = null;
  let activeRequest = null;

  function requestPage(number, offset) {
    const params = new URLSearchParams({
      search_value: number,
      type: "4",
      limit: "100",
      offset: String(offset),
    });
    return new Promise((resolve, reject) => {
      activeRequest = GM_xmlhttpRequest({
        method: "GET",
        url: `https://webapi.115.com/files/search?${params}`,
        anonymous: false,
        timeout: 15000,
        headers: { Accept: "application/json" },
        onload(response) {
          if (response.status !== 200) {
            reject(new Error(`115 请求失败 (${response.status}), 请检查登录或访问限制`));
            return;
          }
          try {
            resolve(parsePage(JSON.parse(response.responseText)));
          } catch (error) {
            reject(error);
          }
        },
        onerror() {
          reject(new Error("无法连接 115, 请检查网络后重试"));
        },
        ontimeout() {
          reject(new Error("115 查询超时, 请重试"));
        },
        onabort() {
          reject(new Error("查询已取消"));
        },
      });
    });
  }

  async function search(number, run) {
    const cached = cache.get(number);
    if (cached && cached.until > Date.now()) return cached.rows;
    if (pending.has(number)) return pending.get(number);
    const job = (async () => {
      const rows = [];
      for (const term of searchTerms(number)) {
        let complete = false;
        for (let offset = 0; offset < 2000; offset += 100) {
          if (run !== generation) throw new Error("查询已取消");
          const page = await requestPage(term, offset);
          const received = offset + page.rows.length;
          rows.push(...page.rows);
          if ((page.count !== null && received >= page.count) || page.rows.length < 100) {
            complete = true;
            break;
          }
        }
        if (!complete) throw new Error("115 搜索结果超过 2000 项, 无法确认全部匹配结果");
      }
      const found = candidates(rows, number);
      cache.set(number, { rows: found, until: Date.now() + (found.length ? 120000 : 15000) });
      return found;
    })();
    pending.set(number, job);
    try {
      return await job;
    } finally {
      if (pending.get(number) === job) pending.delete(number);
    }
  }

  function button(text, click) {
    const node = document.createElement("button");
    node.type = "button";
    node.textContent = text;
    node.style.cssText =
      "padding:8px 12px;border:1px solid #22a67a;border-radius:6px;background:#e8faf2;color:#08744e;cursor:pointer";
    node.addEventListener("click", click);
    return node;
  }

  async function mount() {
    const path = location.pathname;
    if (path === currentPath) return;
    currentPath = path;
    const run = ++generation;
    controller?.abort();
    activeRequest?.abort();
    pending.clear();
    document.getElementById("amane-115-playback")?.remove();
    const match = /^\/meta\/(\d+)\/?$/.exec(path);
    if (!match) return;
    controller = new AbortController();
    const main = document.querySelector("main");
    if (!main) {
      currentPath = "";
      return;
    }
    const panel = document.createElement("section");
    panel.id = "amane-115-playback";
    panel.style.cssText =
      "padding:12px;margin:8px 0;border:1px solid #22a67a;border-radius:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap";
    panel.setAttribute("aria-label", "115 播放");
    const status = document.createElement("span");
    status.setAttribute("role", "status");
    const results = document.createElement("span");
    let number = "";
    async function load() {
      retry.disabled = true;
      results.replaceChildren();
      status.textContent = "正在查询 115…";
      try {
        if (!number) {
          const response = await fetch(`/api/metadata/${match[1]}`, {
            credentials: "same-origin",
            signal: controller.signal,
          });
          if (!response.ok) throw new Error(`无法读取影片信息 (${response.status})`);
          const metadata = await response.json();
          if (typeof metadata.number !== "string" || !metadata.number.trim())
            throw new Error("影片没有可用番号");
          number = metadata.number.trim().toUpperCase();
        }
        if (run !== generation) return;
        const found = await search(number, run);
        if (run !== generation) return;
        status.textContent = found.length
          ? `${number} · 115 匹配 ${found.length} 个文件`
          : `${number} · 未找到文件`;
        if (found.length === 1) {
          results.append(
            button("115 播放", () =>
              GM_openInTab(masterUrl(found[0].pickcode), { active: true, insert: true }),
            ),
          );
        } else if (found.length > 1) {
          const select = document.createElement("select");
          select.setAttribute("aria-label", "115 文件");
          for (const item of found) {
            const option = document.createElement("option");
            option.value = item.pickcode;
            option.textContent = `${item.name}${item.size ? ` · ${(item.size / 1024 ** 3).toFixed(2)} GB` : ""}`;
            select.append(option);
          }
          select.style.maxWidth = "min(70vw, 500px)";
          results.append(
            select,
            button("115 播放", () =>
              GM_openInTab(masterUrl(select.value), { active: true, insert: true }),
            ),
          );
        }
      } catch (error) {
        if (run === generation)
          status.textContent = error instanceof Error ? error.message : "115 查询失败";
      } finally {
        if (run === generation) retry.disabled = false;
      }
    }
    const retry = button("重新查询", () => {
      cache.delete(number);
      void load();
    });
    panel.append(status, results, retry);
    main.prepend(panel);
    void load();
  }

  // 轮询只检查路由与控件存在性, 避免 React 高频 DOM 更新触发重复网络请求.
  setInterval(() => {
    if (
      /^\/meta\/\d+\/?$/.test(location.pathname) &&
      !document.getElementById("amane-115-playback")
    )
      currentPath = "";
    void mount();
  }, 500);
  void mount();
})();
