// Generates resources/icon.png (app icon) and resources/trayTemplate(@2x).png
// with plain zlib, so the repo carries no binary design assets.
import { deflateSync } from 'node:zlib'
import { writeFileSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const out = join(dirname(fileURLToPath(import.meta.url)), '..', 'resources')
mkdirSync(out, { recursive: true })

const crcTable = Array.from({ length: 256 }, (_, n) => {
  let c = n
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
  return c >>> 0
})
const crc32 = (buf) => {
  let c = 0xffffffff
  for (const b of buf) c = crcTable[(c ^ b) & 0xff] ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}
function chunk(type, data) {
  const len = Buffer.alloc(4)
  len.writeUInt32BE(data.length)
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data])
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(body))
  return Buffer.concat([len, body, crc])
}
function png(size, pixel) {
  const raw = Buffer.alloc((size * 4 + 1) * size)
  for (let y = 0; y < size; y++) {
    raw[y * (size * 4 + 1)] = 0
    for (let x = 0; x < size; x++) {
      const [r, g, b, a] = pixel(x + 0.5, y + 0.5, size)
      const o = y * (size * 4 + 1) + 1 + x * 4
      raw[o] = r; raw[o + 1] = g; raw[o + 2] = b; raw[o + 3] = a
    }
  }
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(size, 0); ihdr.writeUInt32BE(size, 4)
  ihdr[8] = 8; ihdr[9] = 6; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0
  return Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', ihdr), chunk('IDAT', deflateSync(raw)), chunk('IEND', Buffer.alloc(0))])
}
const clamp = (v) => Math.max(0, Math.min(1, v))
const cover = (d, aa) => clamp(0.5 - d / aa) // signed distance -> coverage

// App icon: warm orange rounded square, white ring (the community) with a dot (you, inside it).
const icon = png(1024, (x, y, s) => {
  const cx = s / 2, cy = s / 2
  const half = s * 0.42, r = s * 0.2
  const qx = Math.abs(x - cx) - (half - r), qy = Math.abs(y - cy) - (half - r)
  const box = Math.hypot(Math.max(qx, 0), Math.max(qy, 0)) + Math.min(Math.max(qx, qy), 0) - r
  const bg = cover(box, 1.5)
  const d = Math.hypot(x - cx, y - cy)
  const ring = cover(Math.abs(d - s * 0.21) - s * 0.045, 1.5)
  const dot = cover(Math.hypot(x - (cx + s * 0.12), y - (cy - s * 0.12)) - s * 0.06, 1.5)
  const white = Math.max(ring, dot)
  const t = y / s
  const R = 249 - 20 * t, G = 115 - 40 * t, B = 22 + 10 * t
  const r0 = R + (255 - R) * white, g0 = G + (255 - G) * white, b0 = B + (255 - B) * white
  return [Math.round(r0), Math.round(g0), Math.round(b0), Math.round(255 * bg)]
})
writeFileSync(join(out, 'icon.png'), icon)

// Tray (macOS template image: black + alpha only).
for (const [name, size] of [['trayTemplate.png', 16], ['trayTemplate@2x.png', 32]]) {
  writeFileSync(join(out, name), png(size, (x, y, s) => {
    const cx = s / 2, cy = s / 2
    const d = Math.hypot(x - cx, y - cy)
    const ring = cover(Math.abs(d - s * 0.34) - s * 0.08, 1)
    const dot = cover(Math.hypot(x - (cx + s * 0.2), y - (cy - s * 0.2)) - s * 0.12, 1)
    return [0, 0, 0, Math.round(255 * Math.max(ring, dot))]
  }))
}
console.log('icons written to', out)
