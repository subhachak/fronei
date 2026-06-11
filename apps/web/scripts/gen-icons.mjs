import { createCanvas } from 'canvas'
import { writeFileSync } from 'fs'

for (const size of [192, 512]) {
  const canvas = createCanvas(size, size)
  const ctx = canvas.getContext('2d')
  ctx.fillStyle = '#7c3aed'
  ctx.roundRect(0, 0, size, size, size * 0.2)
  ctx.fill()
  ctx.fillStyle = '#ffffff'
  ctx.font = `bold ${size * 0.5}px sans-serif`
  ctx.textAlign = 'center'
  ctx.textBaseline = 'middle'
  ctx.fillText('S', size / 2, size / 2)
  writeFileSync(`public/icon-${size}.png`, canvas.toBuffer('image/png'))
  console.log(`Generated icon-${size}.png`)
}
