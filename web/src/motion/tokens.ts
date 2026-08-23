// Motion timings extracted from design-handoff/Index Prototype.dc.html.
// Durations are in seconds (Motion convention); CSS twins live in index.css @theme.

export const EASE_EDITORIAL = [0.2, 0.7, 0.2, 1] as const
export const EASE_FLIP = [0.2, 0.7, 0.25, 1] as const

export const DURATION = {
  reveal: 0.55, // tile fade + slide-up on scroll into view
  imageLoad: 0.5, // blur-up once the thumb finishes loading
  screenFade: 0.25, // INDEX / LISTS / detail screen switches, scrims, toast
  lightboxIn: 0.28,
  lightboxOut: 0.24,
  lightboxFlip: 0.38, // grid thumb → lightbox shared-element flight
  lightboxNavOut: 0.15, // outgoing image on prev/next
  lightboxNavIn: 0.22, // incoming image on prev/next
  sheetIn: 0.32, // save-to-list bottom sheet
  snapBack: 0.28, // cancelled swipe returning to rest
} as const

export const REVEAL = {
  offsetY: 18, // px slide-up distance
  stagger: 0.06, // s between siblings…
  staggerGroup: 4, // …cycling every 4 tiles: delay = (i % 4) * stagger
  viewportMargin: '60px', // IntersectionObserver rootMargin
} as const

export const IMAGE_LOAD = {
  blur: 14, // px blur while loading (10 in the lightbox)
  scale: 1.04,
} as const

export const GESTURE = {
  dismissOffsetY: 24, // px translate on lightbox close
  swipeNextThreshold: 70, // px horizontal drag to change image
  swipeDismissThreshold: 110, // px downward drag to close lightbox
  axisLockThreshold: 8, // px before a drag commits to an axis
} as const
