import type { WarmrApi } from '../shared/types'

declare global {
  interface Window {
    warmr: WarmrApi
  }
}

export {}
