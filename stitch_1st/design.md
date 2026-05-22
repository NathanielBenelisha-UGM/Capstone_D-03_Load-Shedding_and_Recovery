---
name: Clean White Antigravity
colors:
  surface: '#f6fafe'
  surface-dim: '#d6dade'
  surface-bright: '#f6fafe'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#f0f4f8'
  surface-container: '#eaeef2'
  surface-container-high: '#e4e9ed'
  surface-container-highest: '#dfe3e7'
  on-surface: '#171c1f'
  on-surface-variant: '#444748'
  inverse-surface: '#2c3134'
  inverse-on-surface: '#edf1f5'
  outline: '#747878'
  outline-variant: '#c4c7c8'
  surface-tint: '#5d5f5f'
  primary: '#5d5f5f'
  on-primary: '#ffffff'
  primary-container: '#ffffff'
  on-primary-container: '#747676'
  inverse-primary: '#c6c6c7'
  secondary: '#006e16'
  on-secondary: '#ffffff'
  secondary-container: '#00f93f'
  on-secondary-container: '#006d16'
  tertiary: '#bf0028'
  on-tertiary: '#ffffff'
  tertiary-container: '#ffffff'
  on-tertiary-container: '#ec0034'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#e2e2e2'
  primary-fixed-dim: '#c6c6c7'
  on-primary-fixed: '#1a1c1c'
  on-primary-fixed-variant: '#454747'
  secondary-fixed: '#72ff70'
  secondary-fixed-dim: '#00e639'
  on-secondary-fixed: '#002203'
  on-secondary-fixed-variant: '#00530e'
  tertiary-fixed: '#ffdad8'
  tertiary-fixed-dim: '#ffb3b1'
  on-tertiary-fixed: '#410007'
  on-tertiary-fixed-variant: '#92001c'
  background: '#f6fafe'
  on-background: '#171c1f'
  surface-variant: '#dfe3e7'
typography:
  display-lg:
    fontFamily: Inter
    fontSize: 48px
    fontWeight: '700'
    lineHeight: '1.1'
    letterSpacing: -0.02em
  headline-md:
    fontFamily: Inter
    fontSize: 24px
    fontWeight: '600'
    lineHeight: '1.2'
    letterSpacing: -0.01em
  body-rt:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: '400'
    lineHeight: '1.5'
    letterSpacing: '0'
  data-mono:
    fontFamily: JetBrains Mono
    fontSize: 14px
    fontWeight: '500'
    lineHeight: '1.4'
    letterSpacing: '0'
  label-caps:
    fontFamily: JetBrains Mono
    fontSize: 12px
    fontWeight: '700'
    lineHeight: '1'
    letterSpacing: 0.1em
rounded:
  sm: 0.5rem
  DEFAULT: 1rem
  md: 1.5rem
  lg: 2rem
  xl: 3rem
  full: 9999px
spacing:
  base: 8px
  container-padding: 24px
  gutter-floating: 32px
  margin-screen: 40px
---

## Brand & Style

This design system is built for the next generation of industrial oversight. It merges the clinical precision of SCADA systems with a weightless, futuristic aesthetic. The brand personality is hyper-clean, optimistic, and technologically advanced, moving away from the "dark mode" industrial tropes into a bright, airy "Antigravity" space. 

The visual style is a hybrid of **Minimalism** and **Glassmorphism**. By utilizing "Bubble Glass" surfaces—frosted, translucent layers with soft 3D depth and exaggerated, pill-like curvatures—the UI feels as though it is floating over a fluid, atmospheric background. The emotional response should be one of absolute clarity and calm, even during high-alert industrial scenarios.

## Colors

The palette is anchored by a high-clarity off-white environment. The primary interface material is **Pure White** with varying levels of transparency to create the glass effect. 

- **Primary:** Translucent white (Alpha 40-70%) used for panel surfaces.
- **Success (Normal):** Electric Neon Green (#00FF41). This color must pulse or glow to indicate active, healthy machinery.
- **Alert (Tripped):** Glowing Crimson (#FF073A). Reserved strictly for critical failures, using high saturation to break the minimalist palette.
- **Neutral:** A spectrum of soft grays and light blues (#F0F4F8) used for inactive states and background mesh gradients.

The background should feature a subtle, animated gradient mesh shifting between `#F8FAFC` and `#E2E8F0` to reinforce the sense of "air" and depth behind the floating panels.

## Typography

The typographic system balances human-centric readability with technical precision. 

- **Inter** is the workhorse for all structural UI elements, navigation, and primary headers. It provides a modern, neutral foundation that doesn't distract from data visualization.
- **JetBrains Mono** is utilized for all "active" data points, telemetry logs, and sensor readouts. This distinction ensures the user instantly recognizes the difference between interface controls and live industrial data.

For mobile views, scale `display-lg` down to `32px` and `headline-md` to `20px` to maintain legibility within smaller floating glass containers.

## Layout & Spacing

The layout philosophy follows a **Floating Grid** model. Elements never touch the edges of the browser or screen, reinforcing the "Antigravity" theme. 

- **Gutter Strategy:** Use unusually large gutters (32px+) between main modules to allow the background mesh to breathe through.
- **Padding:** Internal card padding is generous (24px) to emphasize the "Bubble" nature of the glass panels.
- **Connections:** Use smooth cubic-bezier SVG lines to connect floating nodes or sensors. These lines should be thin (1px) and semi-transparent, appearing to drift in 3D space.
- **Responsive Behavior:** On tablet and mobile, the 3rd dimension is flattened slightly; margins reduce to 16px, and floating panels stack vertically while maintaining their translucent properties.

## Elevation & Depth

Depth is the core differentiator of this design system. We use a three-tiered elevation model:

1.  **The Atmosphere (Level 0):** The animated gradient mesh background.
2.  **The Floating Pane (Level 1):** Main UI panels using `backdrop-filter: blur(20px)` and a thin 1px white border at 20% opacity. Shadows are ultra-diffused: `0 20px 40px rgba(0,0,0,0.05)`.
3.  **The Interaction Layer (Level 2):** Active elements or hovered cards. When a user hovers over a panel, it should "lift" via a 3D transform `translateZ(10px)` and the shadow should become deeper and softer.

Avoid hard shadows or solid fills. Every surface must feel permeable by light.

## Shapes

The shape language is organic and "pill-shaped," utilizing high corner radii to achieve a soft-tech look.
- **Base Corner Radius:** 32px (`rounded-lg`) for standard cards to achieve the "Bubble" look.
- **Large Containers:** 48px (`rounded-xl`) for primary floating panes.
- **Status Indicators:** Always perfectly circular to represent physical LEDs or sensor nodes.

Avoid sharp 0px corners entirely, as they break the fluid, weightless metaphor of the system.

## Components

### Buttons & Controls
Buttons are glass capsules (pill-shaped). The "Primary" action button features a subtle inner glow. Hovering on a button should trigger a "bloom" effect where the neon accent color bleeds softly into the surrounding glass.

### Bubble Glass Cards
These are the primary data containers. They must feature a `1px` top-down highlight stroke to simulate light hitting the top edge of a glass pane, with exaggerated 32px+ rounding. 

### Status Chips
Status chips for "Online" or "Tripped" use the Monospace font and are fully rounded. "Online" chips feature a slow-pulsing neon green shadow (glow), while "Tripped" chips use a rapid, sharper crimson flicker.

### Technical Inputs
Input fields are "recessed" glass—instead of a drop shadow, they use an `inset` shadow to appear as though they are carved into the glass surface. All corners are fully rounded to match the button style.

### Data Visualization
Charts and graphs should use the Neon Green and Glowing Crimson for data lines, set against a transparent background. Use gradient fills under area charts that fade from 30% opacity to 0%.