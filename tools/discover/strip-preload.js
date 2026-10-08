'use strict';
/**
 * Мост для полосы управления инструментом разведки.
 *
 * Отдаётся ТОЛЬКО полосе (tools/discover/strip.html) — своей локальной
 * странице. Страница LMS живёт в отдельном BrowserView со своим partition и
 * без preload вовсе: это сторонний сайт, и дотянуться до инструмента он не
 * может. Наружу выставлены четыре действия без аргументов — ни одного места,
 * куда страница могла бы передать свой адрес или своё решение.
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('discover', {
  mark: () => ipcRenderer.invoke('discover:mark'),
  save: () => ipcRenderer.invoke('discover:save'),
  openOut: () => ipcRenderer.invoke('discover:open-out'),
  quit: () => ipcRenderer.invoke('discover:quit'),
  onState: (handler) => {
    if (typeof handler !== 'function') return;
    ipcRenderer.on('discover:state', (_event, state) => {
      try { handler(state); } catch (err) { /* полоса не должна валить процесс */ }
    });
  },
});
