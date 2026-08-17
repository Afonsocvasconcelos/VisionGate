# VisionGate interface research

This redesign combines direct owner feedback (the dashboard felt disorganized and the login looked unrelated) with established interface guidance. It is a heuristic redesign, not a substitute for observing several people use the installed system.

## Findings applied

- **Put live status and the primary task first.** Nielsen Norman Group's usability heuristics call for visible system status, familiar language, consistency, recognition over recall, and removing irrelevant competing information. VisionGate therefore leads with the active camera and door state; configuration stays in Settings. Source: [10 Usability Heuristics](https://www.nngroup.com/articles/ten-usability-heuristics/).
- **Use one clear hierarchy across every screen.** Apple's Human Interface Guidelines recommend a clear visual hierarchy that distinguishes controls from content. Login and dashboard now share the same brand, typography, surfaces, colors, and control styling. Source: [Apple Human Interface Guidelines](https://developer.apple.com/design/human-interface-guidelines/).
- **Adapt the layout instead of shrinking it.** Material Design's canonical layouts use different pane arrangements at different breakpoints. VisionGate keeps a small utility header and changes the camera/controls grid into one column on narrow screens. Source: [Material 3 canonical layouts](https://m3.material.io/foundations/layout/canonical-examples/overview).
- **Start mobile layouts as one column.** The GOV.UK Design System recommends designing small-screen layouts first and limiting content width for readability. VisionGate's phone view uses one reading order: camera, door, authorized targets. Source: [GOV.UK layout guidance](https://design-system.service.gov.uk/styles/layout/).
- **Make touch and keyboard controls forgiving.** WCAG 2.2 recommends 44 by 44 CSS-pixel targets for easier touch operation, visible focus, reflow, and sufficient contrast. Primary controls use at least 44px height and the layout avoids horizontal scrolling at 320px. Sources: [W3C target size guidance](https://www.w3.org/WAI/WCAG22/Understanding/target-size-enhanced), [WCAG 2.2](https://www.w3.org/TR/WCAG22/), and [contrast guidance](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum).
- **Keep sign-in conventional and password-manager friendly.** Inputs retain visible labels, autofill metadata, paste support, a show/hide option, and generic credential errors. Sources: [GOV.UK password input](https://design-system.service.gov.uk/components/password-input/) and [W3C accessible authentication](https://www.w3.org/WAI/WCAG22/Understanding/accessible-authentication-minimum.html).

## Resulting information order

1. Can VisionGate see the entrance and is detection running?
2. Is the door ready, and can it be opened or closed safely?
3. Who is authorized?
4. Configuration and device onboarding only when Settings is opened.

Detection counts, thresholds, event history, and connection details do not compete with daily controls. They remain available to the backend, console, and settings where they are useful for diagnosis.

## Verification criteria

- Login and dashboard use the same shared design system.
- The dashboard reflows without horizontal scrolling at 320, 390, 768, and desktop widths.
- All important buttons and navigation targets are at least 44px high.
- Keyboard focus is visible, dialogs remain usable on a phone, and reduced-motion preferences are honored.
- Camera, access, door, and settings IDs/API flows remain compatible with the existing backend tests.
