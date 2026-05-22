---
name: Interlink Digital
colors:
  surface: '#f9f9ff'
  surface-dim: '#d7dae3'
  surface-bright: '#f9f9ff'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#f1f3fc'
  surface-container: '#ebedf7'
  surface-container-high: '#e6e8f1'
  surface-container-highest: '#e0e2eb'
  on-surface: '#181c22'
  on-surface-variant: '#414753'
  inverse-surface: '#2d3037'
  inverse-on-surface: '#eef0fa'
  outline: '#717785'
  outline-variant: '#c1c6d5'
  surface-tint: '#005db8'
  primary: '#005ab4'
  on-primary: '#ffffff'
  primary-container: '#0a73e0'
  on-primary-container: '#fefcff'
  inverse-primary: '#aac7ff'
  secondary: '#465f88'
  on-secondary: '#ffffff'
  secondary-container: '#b6d0ff'
  on-secondary-container: '#3f5881'
  tertiary: '#964400'
  on-tertiary: '#ffffff'
  tertiary-container: '#bd5700'
  on-tertiary-container: '#fffbff'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#d6e3ff'
  primary-fixed-dim: '#aac7ff'
  on-primary-fixed: '#001b3e'
  on-primary-fixed-variant: '#00458d'
  secondary-fixed: '#d6e3ff'
  secondary-fixed-dim: '#aec7f7'
  on-secondary-fixed: '#001b3d'
  on-secondary-fixed-variant: '#2d476f'
  tertiary-fixed: '#ffdbc9'
  tertiary-fixed-dim: '#ffb68c'
  on-tertiary-fixed: '#321200'
  on-tertiary-fixed-variant: '#763400'
  background: '#f9f9ff'
  on-background: '#181c22'
  surface-variant: '#e0e2eb'
typography:
  headline-lg:
    fontFamily: Inter
    fontSize: 32px
    fontWeight: '600'
    lineHeight: 40px
  headline-md:
    fontFamily: Inter
    fontSize: 24px
    fontWeight: '600'
    lineHeight: 32px
  body-lg:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: '400'
    lineHeight: 24px
  body-md:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '400'
    lineHeight: 20px
  label-md:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: '500'
    lineHeight: 16px
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  base: 8px
  xs: 4px
  sm: 12px
  md: 16px
  lg: 24px
  xl: 32px
  gutter: 16px
  margin: 24px
---

# Interlink Digital Design System

## Brand & Style
Interlink Digital is a professional, reliable, and modern design system. It moves away from the aggressive warmth of the previous iteration toward a balanced, corporate-modern aesthetic that emphasizes trust and clarity. The style is inspired by high-quality modern interfaces, utilizing a clean layout, refined typography, and a "fidelity" color approach that feels precise and intentional. It aims to evoke a sense of technological stability and effortless usability for professional users.

## Colors
The color palette has shifted from warm ochre and orange tones to a dependable blue-centric scheme.

*   **Primary (#1275e2):** A vibrant, professional blue used for core actions and brand recognition.
*   **Secondary (#5f78a3):** A muted, desaturated blue-grey that provides support without competing with the primary action.
*   **Tertiary (#c55b00):** A sophisticated burnt orange used sparingly for accents or highlighting specific data points.
*   **Neutral (#74777f):** A balanced cool grey used for surfaces, borders, and secondary text.

The system uses a "fidelity" color variant, ensuring that derived shades maintain high perceptual accuracy and professional tone.

## Typography
The system has transitioned from Public Sans to **Inter**, a typeface specifically designed for user interfaces. Inter provides superior legibility on screens and a more neutral, modern technical feel.

*   **Headlines:** Set in Inter with medium-to-semibold weights to establish clear hierarchy.
*   **Body:** Set in Inter Regular for maximum readability in data-heavy environments.
*   **Labels:** Set in Inter Medium with slightly tighter tracking for compact UI elements.

## Layout & Spacing
The layout follows a strict 8px grid system. We utilize a fluid grid for web applications to maximize screen real estate, while maintaining consistent 24px outer margins. Components use a spacing scale derived from a 4px base (spacing value 2) to allow for precise alignment of small UI elements like icons and labels.

## Elevation & Depth
Depth is conveyed through tonal layers and soft, ambient shadows. Surfaces use subtle shifts in the neutral palette to indicate hierarchy. Primary actions may have a slight elevation (soft shadow), while containers generally use low-contrast outlines or subtle tonal fills to stay grounded and clean.

## Shapes
The design system has evolved from a sharp, 0px radius style to a **Rounded (Level 2)** style. 

*   Standard components (buttons, inputs) use a **0.5rem (8px)** corner radius.
*   Larger containers like cards use **1rem (16px)**.
*   Extra large elements or modals use **1.5rem (24px)**.

This change softens the interface, making it feel more approachable and aligned with modern software aesthetics.

## Components
*   **Buttons:** Fully rounded (8px) with Primary blue backgrounds. Secondary buttons use the desaturated blue-grey.
*   **Inputs:** Outlined with a 74777f neutral border, 8px corner radius, and Inter body typography.
*   **Cards:** Elevated with very soft shadows and 16px corner radius.
*   **Chips:** Highly rounded (pill-style) using secondary or tertiary accents for status.