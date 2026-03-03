'use strict'
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('electronAPI', {
  // renderer -> main
  send: (msg) => ipcRenderer.send('cgm-msg', msg),

  // main -> renderer (subscribe)
  on: (channel, cb) => {
    ipcRenderer.on(channel, (_event, data) => cb(data))
  },

  // Drag: Y-axis only
  dragStart: (screenY) => ipcRenderer.send('cgm-drag-start', screenY),
  dragMove:  (screenY) => ipcRenderer.send('cgm-drag-move',  screenY),
})
