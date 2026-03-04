'use strict'
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('electronAPI', {
  // renderer -> main
  send: (msg) => ipcRenderer.send('cgm-msg', msg),

  // main -> renderer (subscribe)
  on: (channel, cb) => {
    ipcRenderer.on(channel, (_event, data) => cb(data))
  },

  // Drag: X and Y axes
  dragStart: (screenX, screenY) => ipcRenderer.send('cgm-drag-start', screenX, screenY),
  dragMove:  (screenX, screenY) => ipcRenderer.send('cgm-drag-move',  screenX, screenY),
})
